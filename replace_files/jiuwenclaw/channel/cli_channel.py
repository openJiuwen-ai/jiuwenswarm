# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import inspect
import logging
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.foundation.llm import Model, ProviderType
from openjiuwen.core.foundation.llm.schema.config import (
    ModelClientConfig,
    ModelRequestConfig,
)

from jiuwenclaw.config import (
    get_config,
    get_config_raw,
    resolve_env_vars,
    update_context_engine_enabled_in_config,
    update_memory_forbidden_enabled_in_config,
    update_permissions_enabled_in_config,
    get_model_names,
    get_model_config,
    add_or_update_model_in_config,
    update_default_models_in_config,
    update_preferred_language_in_config,
)
from jiuwenclaw.jiuwen_core_patch import apply_openai_model_client_patch
from jiuwenclaw.gateway.route_binding import GatewayRouteBinding
from jiuwenclaw.version import __version__

logger = logging.getLogger(__name__)
apply_openai_model_client_patch()

# ── 需要转发到 Agent 的方法集合 ──────────────────────────────

CLI_FORWARD_REQ_METHODS = frozenset(
    {
        "command.add_dir",
        "command.chrome",
        "command.compact",
        "command.diff",
        "command.resume",
        "command.session",
        "chat.send",
        "chat.interrupt",
        "chat.resume",
        "chat.user_answer",
        "history.get",
        "browser.start",
        "skills.marketplace.list",
        "skills.list",
        "skills.installed",
        "skills.get",
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
        "skills.evolution.status",
        "skills.evolution.get",
        "skills.evolution.save",
        "extensions.list",
        "extensions.import",
        "extensions.delete",
        "extensions.toggle",
    }
)

CLI_FORWARD_NO_LOCAL_HANDLER_METHODS = frozenset(
    {
        "command.add_dir",
        "command.chrome",
        "command.compact",
        "command.diff",
        "command.resume",
        "command.session",
        "browser.start",
        "skills.marketplace.list",
        "skills.list",
        "skills.installed",
        "skills.get",
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
        "skills.evolution.status",
        "skills.evolution.get",
        "skills.evolution.save",
        "extensions.list",
        "extensions.import",
        "extensions.delete",
        "extensions.toggle",
    }
)


@dataclass
class CliHandlersBindParams:
    channel: Any  # GatewayServer instance
    agent_client: Any = None
    message_handler: Any = None
    on_config_saved: Any = None
    path: str = "/tui"


@dataclass
class CliRouteBindParams:
    agent_client: Any = None
    message_handler: Any = None
    on_config_saved: Any = None
    path: str = "/tui"
    channel_id: str = "tui"


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
}

_CLI_CONFIG_YAML_SETTERS: dict[str, Any] = {
    "context_engine_enabled": update_context_engine_enabled_in_config,
    "permissions_enabled": update_permissions_enabled_in_config,
    "memory_forbidden_enabled": update_memory_forbidden_enabled_in_config,
    "preferred_language": update_preferred_language_in_config,
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
        {"key": "evolution_auto_scan", "label": "自动扫描技能", "group": "Features",
         "type": "toggle", "source": "env", "default": "false"},
    ]


def _normalize_provider_value(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return normalized

    available_model_providers = [provider.value for provider in ProviderType]
    lookup = {provider.lower(): provider for provider in available_model_providers}
    return lookup.get(normalized.lower(), normalized)



async def _clear_agent_config_cache(agent_client=None) -> None:
    try:
        if agent_client is not None:
            from jiuwenclaw.e2a.gateway_normalize import e2a_from_agent_fields
            from jiuwenclaw.schema.message import ReqMethod
            import uuid

            env = e2a_from_agent_fields(
                request_id=f"cfg-reload-{uuid.uuid4().hex[:8]}",
                channel_id="",
                req_method=ReqMethod.AGENT_RELOAD_CONFIG,
            )
            await agent_client.send_request(env)
        else:
            get_config()
    except Exception as e:  # noqa: BLE001
        logger.debug("[cli config.set] clear agent config cache skipped: %s", e)


def _persist_env_updates(updates: dict[str, str]) -> None:
    from jiuwenclaw.utils import get_env_file

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
    from jiuwenclaw.utils import get_env_file

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


def register_cli_handlers(bind: CliHandlersBindParams) -> None:
    channel = bind.channel
    agent_client = bind.agent_client
    on_config_saved = bind.on_config_saved
    path = bind.path

    async def _config_get(ws, req_id, params, session_id):
        payload = {
            param_key: (os.getenv(env_key) or "")
            for param_key, env_key in _CLI_CONFIG_SET_ENV_MAP.items()
        }
        payload["app_version"] = __version__
        try:
            raw = get_config_raw()
            for key, val in payload.items():
                from jiuwenclaw.extensions import ExtensionRegistry

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
        except Exception:
            payload.setdefault("context_engine_enabled", "false")
            payload.setdefault("permissions_enabled", "false")
            payload.setdefault("memory_forbidden_enabled", "false")
            payload.setdefault("preferred_language", "zh")
        payload["schema"] = _build_config_schema()
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _config_set(ws, req_id, params, session_id):
        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST"
            )
            return
        for key, val in params.items():
            from jiuwenclaw.extensions import ExtensionRegistry

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

        if env_updates:
            _persist_env_updates(env_updates)
        if yaml_updated:
            real_client = (
                agent_client.get("value")
                if isinstance(agent_client, dict)
                else agent_client
            )
            await _clear_agent_config_cache(real_client)

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

        model_request_config = ModelRequestConfig(model=model, temperature=0)
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
                response = await _probe(1)
            except Exception as first_exc:  # noqa: BLE001
                logger.info(
                    "[cli config.validate_model] max_tokens=1 failed, retrying with 16: %s",
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

        if not (isinstance(content, str) and content.strip()):
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

    async def _session_list(ws, req_id, params, session_id):
        from jiuwenclaw.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenclaw.schema.message import ReqMethod

        limit = 20
        if isinstance(params, dict):
            raw_limit = params.get("limit")
            if isinstance(raw_limit, int):
                limit = raw_limit
            elif isinstance(raw_limit, str) and raw_limit.strip().isdigit():
                limit = int(raw_limit.strip())
        limit = max(1, min(limit, 200))

        real_client = (
            agent_client.get("value")
            if isinstance(agent_client, dict)
            else agent_client
        )
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
        )
        resp = await real_client.send_request(env)
        if not resp.ok:
            await channel.send_response(ws, req_id, ok=False, error="session.list failed")
            return
        all_sessions = (
            resp.payload.get("sessions", [])
            if isinstance(resp.payload, dict)
            else []
        )
        cli_sessions = [
            s for s in all_sessions if s.get("channel_id", "") == "tui"
        ][:limit]
        await channel.send_response(ws, req_id, ok=True, payload={"sessions": cli_sessions})

    async def _session_create(ws, req_id, params, session_id):
        from jiuwenclaw.utils import get_agent_sessions_dir
        from jiuwenclaw.agentserver.session_metadata import init_session_metadata

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
        workspace_session_dir = get_agent_sessions_dir()
        workspace_session_dir.mkdir(parents=True, exist_ok=True)
        session_dir = workspace_session_dir / target
        if session_dir.exists():
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="session already exists",
                code="ALREADY_EXISTS",
            )
            return
        session_dir.mkdir()
        # 初始化元数据（与 web channel 对齐）
        pd_raw = params.get("project_dir")
        init_project_dir = pd_raw.strip() if isinstance(pd_raw, str) else None
        init_session_metadata(
            session_id=target,
            channel_id="tui",
            title=str(params.get("title") or "").strip(),
            mode=params.get("mode", "unknown"),
            project_dir=init_project_dir,
        )
        await channel.send_response(ws, req_id, ok=True, payload={"session_id": target})

    async def _session_delete(ws, req_id, params, session_id):
        from jiuwenclaw.utils import get_agent_sessions_dir

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
        session_dir = get_agent_sessions_dir() / target
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
        shutil.rmtree(session_dir)
        await channel.send_response(ws, req_id, ok=True, payload={"session_id": target})

    async def _session_rename(ws, req_id, params, session_id):
        """优先经 E2A 转发至 AgentWebSocketServer._handle_session_rename；无 agent 或转发失败时本地回退。"""
        from jiuwenclaw.agentserver.session_rename import apply_session_rename
        from jiuwenclaw.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenclaw.schema.message import ReqMethod

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
                    req_method=ReqMethod.SESSION_RENAME,
                    params=params if isinstance(params, dict) else {},
                    is_stream=False,
                    timestamp=time.time(),
                )
                resp = await real_client.send_request(env)
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

    async def _command_model(ws, req_id, params, session_id):
        from jiuwenclaw.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenclaw.schema.message import ReqMethod

        if not isinstance(params, dict):
            params = {}
        action = params.get("action")
        model_name = params.get("model")

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

        if action == "add_model":
            target = str(params.get("target", "")).strip()
            configs = params.get("config", {})
            if not target:
                await channel.send_response(
                    ws, req_id, ok=False, error="Target model name (target) is required"
                )
                return
            client_cfg = {}
            key_map = {
                "model": "model_name",
                "provider": "client_provider",
                "api_key": "api_key",
                "api_base": "api_base",
                "url": "api_base",
                "base_url": "api_base",
                "timeout": "timeout",
                "verify_ssl": "verify_ssl",
                "ssl_cert": "ssl_cert",
            }
            # target 可能是 "model=gpt-5" 形式（前端把第一个 key=value 当作 name 参数解析）
            if "=" in target:
                _eq = target.index("=")
                _k, _v = target[:_eq].strip().lower(), target[_eq + 1:].strip()
                client_cfg[key_map.get(_k, _k)] = _v
                if _k in ("model", "model_name"):
                    target = _v
            for k, v in configs.items():
                mapped_k = key_map.get(k.lower(), k)
                client_cfg[mapped_k] = v
            if "verify_ssl" not in client_cfg:
                client_cfg["verify_ssl"] = False
            if "timeout" not in client_cfg:
                client_cfg["timeout"] = 1800
            model_cfg_obj = configs.get("model_config_obj", {})
            if not model_cfg_obj:
                model_cfg_obj = {"temperature": 0.95}
            # target 作为 model_name 的回退：若未通过 model= 参数指定，则以 target 为准
            if not client_cfg.get("model_name"):
                client_cfg["model_name"] = target
            effective_name = client_cfg["model_name"]

            new_entry = {
                "model_client_config": client_cfg,
                "model_config_obj": model_cfg_obj,
            }
            try:
                _raw = get_config_raw()
                _raw_defs = (_raw.get("models") or {}).get("defaults")
                if isinstance(_raw_defs, list):
                    # 重名校验
                    existing_names = [
                        resolve_env_vars(str((e.get("model_client_config") or {}).get("model_name", "")))
                        for e in _raw_defs if isinstance(e, dict)
                    ]
                    if effective_name in existing_names:
                        await channel.send_response(
                            ws, req_id, ok=False,
                            error=f"Model '{effective_name}' already exists.",
                        )
                        return
                    _raw_defs.append(new_entry)
                    update_default_models_in_config(_raw_defs)
                else:
                    add_or_update_model_in_config(target, new_entry)
                logger.info(
                    "[cli command.model] 新增模型: name=%s, "
                    "client_cfg=%s, model_config_obj=%s",
                    effective_name, client_cfg, model_cfg_obj,
                )
            except Exception as e:
                await channel.send_response(ws, req_id, ok=False, error=str(e))
                return
            _reload_env = e2a_from_agent_fields(
                request_id=req_id,
                channel_id="cli",
                session_id=session_id,
                req_method=ReqMethod.AGENT_RELOAD_CONFIG,
                params={},
                is_stream=False,
                timestamp=time.time(),
            )
            await real_client.send_request(_reload_env)
            await channel.send_response(
                ws, req_id, ok=True,
                payload={"type": "model_added", "name": target},
            )
            return

        if not model_name or not str(model_name).strip():
            names = get_model_names()
            logger.info(
                "[cli command.model] 列出模型: names=%s, current=%s",
                names,
                os.getenv("MODEL_NAME", "unknown"),
            )
            env = e2a_from_agent_fields(
                request_id=req_id,
                channel_id="cli",
                session_id=session_id,
                req_method=ReqMethod.COMMAND_MODEL,
                params={},
                is_stream=False,
                timestamp=time.time(),
            )
            resp = await real_client.send_request(env)
            payload = resp.payload if resp.ok else {}
            payload["available_models"] = names
            _raw = get_config_raw()
            _defs = (_raw.get("models") or {}).get("defaults")
            if isinstance(_defs, list) and _defs:
                _first_name = resolve_env_vars(str((_defs[0].get("model_client_config") or {}).get("model_name", "")))
                payload["current"] = _first_name or os.getenv("MODEL_NAME", "unknown")
            else:
                payload["current"] = os.getenv("MODEL_NAME", "unknown")
            await channel.send_response(ws, req_id, ok=True, payload=payload)
            return

        target = str(model_name).strip()
        logger.info("[cli command.model] 切换模型: target=%s", target)
        if target not in get_model_names():
            logger.warning(
                "[cli command.model] 模型不存在: %s, 可用: %s",
                target,
                get_model_names(),
            )
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=(
                    f"Model '{target}' not found. "
                    f"Available: {', '.join(get_model_names())}"
                ),
            )
            return

        _raw_cfg = get_config_raw()
        _raw_defaults = (_raw_cfg.get("models") or {}).get("defaults")
        if isinstance(_raw_defaults, list):
            _target_entry = None
            _other_entries = []
            for _e in _raw_defaults:
                if not isinstance(_e, dict):
                    _other_entries.append(_e)
                    continue
                _ename = resolve_env_vars(str((_e.get("model_client_config") or {}).get("model_name", "")))
                if _ename == target:
                    _target_entry = _e
                else:
                    _other_entries.append(_e)
            if _target_entry is None:
                await channel.send_response(ws, req_id, ok=False, error=f"Model '{target}' config not found")
                return
            update_default_models_in_config([_target_entry] + _other_entries)
            logger.info("[cli command.model] 新格式切换，已更新 models.defaults 首位: %s", target)
            _reload_env = e2a_from_agent_fields(
                request_id=req_id,
                channel_id="cli",
                session_id=session_id,
                req_method=ReqMethod.AGENT_RELOAD_CONFIG,
                params={},
                is_stream=False,
                timestamp=time.time(),
            )
            await real_client.send_request(_reload_env)
            if on_config_saved:
                try:
                    _cb = on_config_saved(set(), env_updates={}, config_payload=get_config())
                    if inspect.isawaitable(_cb):
                        await _cb
                except Exception as _e2:
                    logger.warning("[cli model.switch] on_config_saved failed: %s", _e2)
            logger.info("[cli command.model] 切换完成(新格式): current=%s", target)
            await channel.send_response(ws, req_id, ok=True, payload={
                "current": target,
                "requested": target,
                "type": "switched",
                "applied": True,
            })
            return

        env_from_file = _load_env_from_file()
        raw_model_cfg = get_model_config(target)
        logger.info("[cli command.model] 模型 '%s' 原始配置: %s", target, raw_model_cfg)
        if not raw_model_cfg:
            await channel.send_response(
                ws, req_id, ok=False, error=f"Model '{target}' config not found"
            )
            return
        raw_client_cfg = raw_model_cfg.get("model_client_config", {})
        raw_model_config_obj = raw_model_cfg.get("model_config_obj", {})
        if not raw_client_cfg:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=f"Model '{target}' has no model_client_config",
            )
            return

        import re as _re

        pattern = _re.compile(r"\$\{([^:}]+)(?::-([^}]*))?\}")
        resolved_cfg = {}
        unresolved_env_vars = {}
        for key, raw_val in raw_client_cfg.items():
            if not isinstance(raw_val, str):
                resolved_cfg[key] = raw_val
                continue

            def _replace(match):
                var_name = match.group(1)
                default = match.group(2)
                if var_name in env_from_file:
                    return env_from_file[var_name]
                if default is not None:
                    return default
                unresolved_env_vars[var_name] = True
                return ""

            resolved_cfg[key] = pattern.sub(_replace, raw_val)

        logger.info("[cli command.model] 解析后的配置: %s", resolved_cfg)

        required_keys = {
            "api_base": "API_BASE",
            "api_key": "API_KEY",
            "model_name": "MODEL_NAME",
            "client_provider": "MODEL_PROVIDER",
        }
        missing = []
        for yaml_key, env_key in required_keys.items():
            val = resolved_cfg.get(yaml_key, "")
            if not val:
                is_env_ref = (
                    yaml_key in raw_client_cfg
                    and isinstance(raw_client_cfg[yaml_key], str)
                    and raw_client_cfg[yaml_key].startswith("${")
                )
                if is_env_ref:
                    env_var_in_raw = raw_client_cfg[yaml_key]
                    var_names_in_val = _re.findall(
                        r"\$\{([^:}]+)(?::-([^}]*))?\}", env_var_in_raw
                    )
                    for vn, vd in var_names_in_val:
                        env_file_val = env_from_file.get(vn, "")
                        if not env_file_val and (vd is None or vd == ""):
                            missing.append(f"{yaml_key} (env var {vn} not set)")
                else:
                    missing.append(yaml_key)
        if missing:
            logger.error("[cli command.model] 必要配置缺失: %s, 无法切换", missing)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=(
                    f"Model '{target}' missing required config: {', '.join(missing)}. "
                    "Please set the corresponding environment variables."
                ),
            )
            return

        switch_env_map = {
            "model_name": "MODEL_NAME",
            "client_provider": "MODEL_PROVIDER",
            "api_key": "API_KEY",
            "api_base": "API_BASE",
        }
        env_updates = {}
        for yaml_key, env_key in switch_env_map.items():
            if yaml_key in resolved_cfg and resolved_cfg[yaml_key]:
                env_updates[env_key] = str(resolved_cfg[yaml_key])
        if not env_updates:
            await channel.send_response(ws, req_id, ok=False, error="No valid config to switch")
            return

        logger.info(
            "[cli command.model] 写入环境变量: %s",
            {k: (v if k != "API_KEY" else "***") for k, v in env_updates.items()},
        )

        env = e2a_from_agent_fields(
            request_id=req_id,
            channel_id="cli",
            session_id=session_id,
            req_method=ReqMethod.COMMAND_MODEL,
            params={
                "action": "switch_model",
                "model": target,
                "env_updates": env_updates,
            },
            is_stream=False,
            timestamp=time.time(),
        )
        resp = await real_client.send_request(env)

        if resp.ok:
            for k, v in env_updates.items():
                os.environ[k] = v
            _persist_env_updates(env_updates)
            try:
                config_templates = {
                    "api_base": "${API_BASE}",
                    "api_key": "${API_KEY}",
                    "model_name": "${MODEL_NAME}",
                    "client_provider": "${MODEL_PROVIDER}",
                }
                config_templates["verify_ssl"] = resolved_cfg.get("verify_ssl", False)
                if "timeout" in resolved_cfg:
                    config_templates["timeout"] = resolved_cfg["timeout"]
                add_or_update_model_in_config(
                    "default",
                    {
                        "model_client_config": config_templates,
                        "model_config_obj": raw_model_config_obj,
                    },
                )
                logger.info("[cli command.model] 已重置 models.default 为环境变量引用")
            except Exception as e:
                logger.warning("[cli command.model] 更新 config.yaml 失败: %s", e)
            if on_config_saved:
                config_payload = get_config()
                try:
                    callback_result = on_config_saved(
                        set(env_updates.keys()),
                        env_updates=dict(env_updates),
                        config_payload=config_payload,
                    )
                    if inspect.isawaitable(callback_result):
                        await callback_result
                except Exception as e:
                    logger.warning("[cli model.switch] on_config_saved failed: %s", e)
            logger.info(
                "[cli command.model] 切换完成: current=%s, requested=%s",
                env_updates.get("MODEL_NAME", target),
                target,
            )
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload={
                    "current": env_updates.get("MODEL_NAME", target),
                    "requested": target,
                    "type": "switched",
                    "applied": True,
                },
            )
        else:
            logger.error("[cli command.model] agentserver 切换失败: %s", resp.error)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=resp.error or "Model switch failed on agent server",
            )

    channel.register_local_handler(path, "config.get", _config_get)
    channel.register_local_handler(path, "config.set", _config_set)
    channel.register_local_handler(path, "config.validate_model", _config_validate_model)
    channel.register_local_handler(path, "session.list", _session_list)
    channel.register_local_handler(path, "session.create", _session_create)
    channel.register_local_handler(path, "session.delete", _session_delete)
    channel.register_local_handler(path, "session.rename", _session_rename)
    channel.register_local_handler(path, "chat.send", _chat_send)
    channel.register_local_handler(path, "chat.resume", _chat_resume)
    channel.register_local_handler(path, "chat.interrupt", _chat_interrupt)
    channel.register_local_handler(path, "chat.user_answer", _chat_user_answer)
    channel.register_local_handler(path, "history.get", _history_get)
    channel.register_local_handler(path, "command.model", _command_model)


def build_cli_route_binding(bind: CliRouteBindParams) -> GatewayRouteBinding:
    def _install(channel: Any) -> None:
        register_cli_handlers(
            CliHandlersBindParams(
                channel=channel,
                agent_client=bind.agent_client,
                message_handler=bind.message_handler,
                on_config_saved=bind.on_config_saved,
                path=bind.path,
            )
        )

    async def _tui_disconnect(_ws: Any, stale_session_keys: list[tuple[str, str]]) -> None:
        mh = bind.message_handler
        if mh is None or not stale_session_keys:
            return
        await mh.cancel_agent_sessions_on_disconnect(stale_session_keys)

    return GatewayRouteBinding(
        path=bind.path,
        channel_id=bind.channel_id,
        forward_methods=CLI_FORWARD_REQ_METHODS,
        forward_no_local_handler_methods=CLI_FORWARD_NO_LOCAL_HANDLER_METHODS,
        install=_install,
        disconnect_handler=_tui_disconnect,
    )
