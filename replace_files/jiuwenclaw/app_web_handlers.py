# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""WebChannel RPC handlers and shared constants (used by app gateway; single source with app.py)."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
import psutil
from openjiuwen.core.common.logging import LogManager
from openjiuwen.core.foundation.llm import Model, ProviderType
from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
from jiuwenclaw.extensions.extension_config_sync import update_extensions_in_config

from jiuwenclaw.config import (
    get_config,
    get_config_raw,
    get_default_models,
    update_default_models_in_config,
    update_heartbeat_in_config,
    update_channel_in_config,
    update_browser_in_config,
    update_preferred_language_in_config,
    update_context_engine_enabled_in_config,
    update_kv_cache_affinity_enabled_in_config,
    update_permissions_enabled_in_config,
    update_disabled_tools_in_config,
    update_memory_forbidden_enabled_in_config,
    update_memory_forbidden_description_in_config,
    update_updater_in_config,
)
from jiuwenclaw.jiuwen_core_patch import apply_openai_model_client_patch
from jiuwenclaw.updater import WindowsUpdaterService
from jiuwenclaw.agentserver.session_id_safe import (
    normalize_safe_session_id,
    resolve_session_dir_under_root,
)
from jiuwenclaw.utils import (
    get_env_file,
    get_root_dir,
    get_agent_sessions_dir,
    get_multi_tenant_user_workspace_dir,
)
from jiuwenclaw.agentserver.skilldev.session_history.restore_chunks import (
    RestoreChunkDecodeError,
    decode_restore_payload_from_stream_with_retry,
)
from jiuwenclaw.agentserver.skilldev.file_read_chunks import (
    FileReadChunkDecodeError,
    decode_file_read_payload_from_stream_with_retry,
)
from jiuwenclaw.agentserver.skilldev.file_write_chunks import (
    FileWriteChunkError,
    FileWriteTransportError,
    send_file_write_with_chunks,
)
from jiuwenclaw.version import __version__
from jiuwenclaw.local_env_config import decrypt, encrypt

apply_openai_model_client_patch()

for _jiuwen_log in LogManager.get_all_loggers().values():
    _jiuwen_log.set_level(logging.CRITICAL)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = get_root_dir()
_ENV_FILE = get_env_file()
load_dotenv(dotenv_path=_ENV_FILE)


def _resolve_env_var_str(value: str) -> str:
    """Resolve ${VAR:-default} patterns in a string using current environment."""
    return re.sub(
        r'\$\{([^:}]+)(?::-([^}]*))?\}',
        lambda m: os.getenv(m.group(1), m.group(2) if m.group(2) is not None else ""),
        value,
    )


def _entry_model_name(entry: dict) -> str:
    """从模型配置条目中提取并解析 model_name。"""
    raw = entry.get("model_client_config", {}).get("model_name", "")
    return _resolve_env_var_str(raw)


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
    "acp.tool_response",
    "chat.send",
    "chat.interrupt",
    "chat.resume",
    "chat.user_answer",
    "history.get",
    "browser.start",
    # "tts.synthesize",
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
    "skilldev.chat",
    "skilldev.start",
    "skilldev.respond",
    "skilldev.status",
    "skilldev.session.list",
    "skilldev.parse_skill",
    "skilldev.download",
    "skilldev.cancel",
    "skilldev.file.list",
    "skilldev.user_answer",
    "tools.add",
})

_FORWARD_NO_LOCAL_HANDLER_METHODS = frozenset({
    "initialize",
    "session.create",
    "acp.tool_response",
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
    "skilldev.chat",
    "skilldev.start",
    "skilldev.respond",
    "skilldev.status",
    "skilldev.session.list",
    "skilldev.parse_skill",
    "skilldev.download",
    "skilldev.file.list",
    "skilldev.user_answer",
    "tools.add",
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
    "evolution_auto_scan": "EVOLUTION_AUTO_SCAN",
    "free_search_ddg_enabled": "FREE_SEARCH_DDG_ENABLED",
    "free_search_bing_enabled": "FREE_SEARCH_BING_ENABLED",
    "free_search_proxy_url": "FREE_SEARCH_PROXY_URL",
    "deepsearch_llm_model_name": "LLM_MODEL_NAME",
    "deepsearch_llm_model_type": "LLM_MODEL_TYPE",
    "deepsearch_llm_base_url": "LLM_BASE_URL",
    "deepsearch_llm_api_key": "LLM_API_KEY",
    "deepsearch_web_search_engine_name": "WEB_SEARCH_ENGINE_NAME",
    "deepsearch_web_search_api_key": "WEB_SEARCH_API_KEY",
    "deepsearch_web_search_url": "WEB_SEARCH_URL",
    "deepsearch_execution_method": "EXECUTION_METHOD",
}
# 配置项键名列表，用于日志等说明
CONFIG_KEYS = tuple(_CONFIG_SET_ENV_MAP.keys())

# 来自 config.yaml 的配置项（前端 param 名 -> config.yaml 路径）
_CONFIG_YAML_KEYS = frozenset({
    "context_engine_enabled",
    "kv_cache_affinity_enabled",
    "permissions_enabled",
    "memory_forbidden_enabled",
    "memory_forbidden_description",
    "disabled_tools",  # 新增
})


async def _clear_agent_config_cache(agent_client=None) -> None:
    """写回 config.yaml 后清除 agent 侧配置缓存，使下次读取时得到最新文件内容。"""
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
    except Exception:  # noqa: BLE001
        pass


def _make_session_id() -> str:
    # 与前端 generateSessionId 保持一致：毫秒时间戳(16进制) + 6位随机16进制
    ts = format(int(time.time() * 1000), "x")
    suffix = secrets.token_hex(3)
    return f"sess_{ts}_{suffix}"


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
    updater_service: WindowsUpdaterService | None = None


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

    from jiuwenclaw.schema.message import Message, EventType

    def _resolve(ref, key="value"):
        """若为 ref 字典则取 key（无则返回 None），否则返回自身。"""
        if isinstance(ref, dict):
            return ref.get(key)
        return ref

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
        sid = _make_session_id()
        
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

    async def _config_get(ws, req_id, params, session_id):
        # 返回 _CONFIG_SET_ENV_MAP 里所有键对应的环境变量当前值
        payload = {
            param_key: (os.getenv(env_key) or "")
            for param_key, env_key in _CONFIG_SET_ENV_MAP.items()
        }
        payload["app_version"] = __version__
        # 合并 config.yaml 中的配置项
        try:
            raw = get_config_raw()
            for key, val in payload.items():
                payload[key] = decrypt(key, val)
            ctx_cfg = (raw.get("react") or {}).get("context_engine_config") or {}
            payload["context_engine_enabled"] = "true" if ctx_cfg.get("enabled", False) else "false"
            payload["kv_cache_affinity_enabled"] = (
                "true" if ctx_cfg.get("enable_kv_cache_release", False) else "false"
            )
            perm_cfg = raw.get("permissions") or {}
            payload["permissions_enabled"] = "true" if perm_cfg.get("enabled", False) else "false"
            memory_cfg = (raw.get("memory") or {}).get("forbidden_memory_definition") or {}
            payload["memory_forbidden_enabled"] = "true" if memory_cfg.get("enabled", False) else "false"
            memory_desc = memory_cfg.get("description") or {}
            preferred_lang = raw.get("preferred_language", "zh")
            payload["memory_forbidden_description"] = memory_desc.get(preferred_lang, memory_desc.get("zh", ""))
            # disabled_tools 数组
            react_cfg = raw.get("react") or {}
            disabled_tools = react_cfg.get("disabled_tools") or []
            payload["disabled_tools"] = disabled_tools
            if not payload.get("free_search_ddg_enabled"):
                payload["free_search_ddg_enabled"] = "true"
            if not payload.get("free_search_bing_enabled"):
                payload["free_search_bing_enabled"] = "true"
            deepsearch_defaults = [
                ("deepsearch_web_search_engine_name", "tavily"),
                ("deepsearch_execution_method", "dependency_driving"),
            ]
            for key, val in deepsearch_defaults:
                if not payload.get(key):
                    payload[key] = val
            gateway_cfg = raw.get("gateway") or {}
            payload["gateway_web_session_storage"] = str(
                gateway_cfg.get("web_session_storage") or "local"
            ).strip().lower()
        except Exception:  # noqa: BLE001
            payload.setdefault("context_engine_enabled", "false")
            payload.setdefault("kv_cache_affinity_enabled", "false")
            payload.setdefault("permissions_enabled", "false")
            payload.setdefault("memory_forbidden_enabled", "false")
            payload.setdefault("memory_forbidden_description", "")
            payload.setdefault("free_search_ddg_enabled", "true")
            payload.setdefault("free_search_bing_enabled", "true")
            payload.setdefault("deepsearch_web_search_engine_name", "tavily")
            payload.setdefault("deepsearch_execution_method", "dependency_driving")
            payload.setdefault("disabled_tools", [])
            payload.setdefault("gateway_web_session_storage", "local")
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
                        # 对持久化到.env的敏感数据进行加密
                        value = encrypt(env_key, value)
                        new_lines.append(f'{env_key}="{value}"\n' if value else f"{env_key}=\n")
                        found = True
                        break
                if not found:
                    new_lines.append(line)
            for env_key, value in updates.items():
                if not any(s.strip().startswith(env_key + "=") for s in new_lines):
                    value = encrypt(env_key, value)
                    new_lines.append(f'{env_key}="{value}"\n' if value else f"{env_key}=\n")
            env_path.parent.mkdir(parents=True, exist_ok=True)
            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
        except OSError as e:
            logger.warning("[config.set] 写回 .env 失败: %s", e)

    async def _config_set(ws, req_id, params, session_id):
        """根据前端消息内容更新配置（支持 .env 与 config.yaml 中的键），并写回对应文件。"""
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        env_updates: dict[str, str] = {}
        yaml_updated: list[str] = []
        available_model_providers = [provider.value for provider in ProviderType]

        for param_key, env_key in _CONFIG_SET_ENV_MAP.items():
            if param_key not in params:
                continue
            val = params[param_key]
            if param_key.endswith("_provider") and val and val not in available_model_providers:
                await channel.send_response(
                    ws, req_id, ok=False,
                    error=f"Model provider must in: {available_model_providers} ",
                    code="BAD_REQUEST"
                )
                return
            if val is None:
                # 改为全量更新, 将env配置全部同步到agentserver
                env_updates[env_key] = os.getenv(env_key)
            else:
                env_updates[env_key] = str(val).strip()

        raw = get_config_raw()
        preferred_lang = raw.get("preferred_language", "zh")

        for param_key in _CONFIG_YAML_KEYS:
            if param_key not in params:
                continue
            val = params[param_key]
            parsed = str(val).strip().lower() in ("true", "1", "yes")
            try:
                if param_key == "context_engine_enabled":
                    update_context_engine_enabled_in_config(parsed)
                elif param_key == "kv_cache_affinity_enabled":
                    update_kv_cache_affinity_enabled_in_config(parsed)
                elif param_key == "permissions_enabled":
                    update_permissions_enabled_in_config(parsed)
                elif param_key == "memory_forbidden_enabled":
                    update_memory_forbidden_enabled_in_config(parsed)
                elif param_key == "memory_forbidden_description":
                    desc_val = str(val).strip()
                    update_memory_forbidden_description_in_config({preferred_lang: desc_val})
                elif param_key == "disabled_tools":
                    # Accept either raw list or JSON string (from frontend normalizeConfigValue)
                    if isinstance(val, list):
                        update_disabled_tools_in_config(val)
                    elif isinstance(val, str):
                        try:
                            parsed_list = json.loads(val)
                            if isinstance(parsed_list, list):
                                update_disabled_tools_in_config(parsed_list)
                            else:
                                logger.warning("[config.set] disabled_tools JSON must decode to list, got: %s", type(
                                    parsed_list).__name__)
                                continue
                        except json.JSONDecodeError:
                            logger.warning(
                                "[config.set] disabled_tools must be valid JSON list: %s", val[:50])
                            continue
                    else:
                        logger.warning(
                            "[config.set] disabled_tools must be a list or JSON string, got: %s", type(val).__name__)
                        continue  # Skip yaml_updated.append on validation failure
                yaml_updated.append(param_key)
            except Exception as e:  # noqa: BLE001
                logger.warning("[config.set] 写回 config.yaml 失败 %s: %s", param_key, e)

        ext_configs = params.get("extension_configs", None)
        ext_security = params.get("extension_security_configs", None)
        if not isinstance(ext_configs, str) and ext_configs is not None:
            raise ValueError("extension_configs must be strings")
        if not isinstance(ext_security, str) and ext_security is not None:
            raise ValueError("extension_security_configs must be strings")
        if ext_configs is not None or ext_security is not None:
            try:
                update_extensions_in_config(ext_configs, ext_security)
                if ext_configs is not None:
                    yaml_updated.append("extension_configs")
                if ext_security is not None:
                    yaml_updated.append("extension_security_configs")
            except RuntimeError as e:
                await channel.send_response(
                    ws, req_id, ok=False, error=str(e), code="BAD_REQUEST",
                )
                return

        for env_key, value in env_updates.items():
            os.environ[env_key] = encrypt(env_key, value)
        applied_without_restart = True

        if env_updates:
            _persist_env_updates(env_updates)
            logger.info("[config.set] 已更新 .env: %s", list(env_updates.keys()))
        if yaml_updated:
            await _clear_agent_config_cache(_resolve(agent_client))
            logger.info("[config.set] 已更新 config.yaml: %s", yaml_updated)

        updated_param_keys = [k for k, e in _CONFIG_SET_ENV_MAP.items() if e in env_updates] + yaml_updated
        await channel.send_response(
            ws, req_id, ok=True,
            payload={"updated": updated_param_keys, "applied_without_restart": applied_without_restart},
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
                    logger.warning("[config.set] on_config_saved failed: %s", e)

    async def _config_validate_model(ws, req_id, params, session_id, max_tokens_bounds=None):
        """Send a minimal chat completion (user message \"Hi\") using draft default-model fields.

        Tries ``max_tokens=infimum_max_tokens`` first to limit cost; if the API rejects it (e.g. minimum output length),
        retries with ``max_tokens=supremum_max_tokens``.
        """
        if max_tokens_bounds is None:
            max_tokens_bounds = {
                "infimum_max_tokens": 1,
                "supremum_max_tokens": 16,
            }

        if isinstance(max_tokens_bounds, dict):
            infimum_max_tokens = max_tokens_bounds.get("infimum_max_tokens")
            supremum_max_tokens = max_tokens_bounds.get("supremum_max_tokens")
        else:
            infimum_max_tokens = 1
            supremum_max_tokens = 16

        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        api_base = str(params.get("api_base") or "").strip()
        api_key = str(params.get("api_key") or "").strip()
        model = str(params.get("model") or "").strip()
        model_provider = str(params.get("model_provider") or "").strip()
        if not all([api_base, api_key, model, model_provider]):
            await channel.send_response(
                ws, req_id, ok=False,
                error="api_base, api_key, model, and model_provider are required",
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
        if api_base.endswith("/chat/completions"):
            api_base = api_base.rsplit("/chat/completions", 1)[0]
        api_base = api_base.rstrip("/")

        verify_ssl = bool(params.get("verify_ssl", False))

        model_request_config = ModelRequestConfig(
            model=model,
            temperature=0,
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
                temperature=0,
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
        if not (isinstance(content, str) and content.strip()):
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

    def _get_raw_model_list() -> list[dict]:
        """从 config.yaml 原始内容读取模型列表（api_key 保持加密态）。"""
        raw = get_config_raw()
        raw_models = raw.get("models", {})
        if "defaults" in raw_models and isinstance(raw_models["defaults"], list) and raw_models["defaults"]:
            return list(raw_models["defaults"])
        if "default" in raw_models and isinstance(raw_models["default"], dict):
            return [dict(raw_models["default"])]
        return []

    async def _models_list(ws, req_id, params, session_id):
        """返回已配置的所有默认模型列表（与 config.get 一致，返回解密后的完整值）。"""
        try:
            config = get_config()
            models = get_default_models(config)
            result = []
            for entry in models:
                mcc = entry.get("model_client_config", {})
                mco = entry.get("model_config_obj", {})
                result.append({
                    "model_name": mcc.get("model_name", ""),
                    "api_base": mcc.get("api_base", ""),
                    "api_key": mcc.get("api_key", ""),
                    "model_provider": mcc.get("client_provider", ""),
                    "temperature": mco.get("temperature", 0.95),
                })
            active_model = result[0]["model_name"] if result else ""
            await channel.send_response(ws, req_id, ok=True, payload={
                "models": result,
                "active_model": active_model,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("[models.list] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _models_save(ws, req_id, params, session_id):
        """新增或更新一个模型配置。model_name 已存在则更新，否则新增。

        支持原子性重命名：传入 original_model_name 时，会先删除旧模型再添加新模型。
        """
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        model_name = str(params.get("model_name") or "").strip()
        if not model_name:
            await channel.send_response(ws, req_id, ok=False, error="model_name is required", code="BAD_REQUEST")
            return
        # 支持 original_model_name 参数用于原子性重命名
        _raw_original = params.get("original_model_name")
        original_model_name = str(_raw_original).strip() if _raw_original is not None else None
        if original_model_name == "":
            original_model_name = None
        api_base = str(params.get("api_base") or "").strip()
        api_key = str(params.get("api_key") or "").strip()
        model_provider = str(params.get("model_provider") or "").strip()
        temperature = float(params.get("temperature", 0.95))

        available_model_providers = [p.value for p in ProviderType]
        if model_provider and model_provider not in available_model_providers:
            await channel.send_response(
                ws, req_id, ok=False,
                error=f"Model provider must be one of: {available_model_providers}",
                code="BAD_REQUEST",
            )
            return

        # 加密 api_key（与 config.set 行为一致）
        if api_key:
            from jiuwenclaw.extensions.registry import ExtensionRegistry
            crypto = ExtensionRegistry.get_instance().get_crypto_provider()
            if crypto:
                api_key = crypto.encrypt(api_key)

        new_entry = {
            "model_client_config": {
                "api_base": api_base,
                "api_key": api_key,
                "model_name": model_name,
                "client_provider": model_provider,
                "timeout": int(params.get("timeout", 1800)),
                "verify_ssl": bool(params.get("verify_ssl", False)),
            },
            "model_config_obj": {
                "temperature": temperature,
            },
        }

        try:
            # 读取原始列表（api_key 保持加密态，不解密）
            models = _get_raw_model_list()

            action = "created"
            # 如果是重命名操作，先删除旧模型
            if original_model_name and original_model_name != model_name:
                models = [m for m in models if _entry_model_name(m) != original_model_name]
                action = "renamed"

            # 按 model_name 查找并更新
            for i, entry in enumerate(models):
                if _entry_model_name(entry) == model_name:
                    models[i] = new_entry
                    if action != "renamed":
                        action = "updated"
                    break
            else:
                models.append(new_entry)

            update_default_models_in_config(models)

            await _clear_agent_config_cache(_resolve(agent_client))
            if on_config_saved:
                config_payload = get_config()
                callback_result = on_config_saved(
                    set(),
                    env_updates={},
                    config_payload=config_payload,
                )
                if inspect.isawaitable(callback_result):
                    await callback_result

            await channel.send_response(ws, req_id, ok=True, payload={
                "model_name": model_name,
                "action": action,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("[models.save] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _models_remove(ws, req_id, params, session_id):
        """删除一个模型配置。至少保留一个模型。"""
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        model_name = str(params.get("model_name") or "").strip()
        if not model_name:
            await channel.send_response(ws, req_id, ok=False, error="model_name is required", code="BAD_REQUEST")
            return

        try:
            models = _get_raw_model_list()
            if len(models) <= 1:
                await channel.send_response(
                    ws, req_id, ok=False,
                    error="At least one model configuration is required",
                    code="BAD_REQUEST",
                )
                return

            new_models = [m for m in models if _entry_model_name(m) != model_name]
            if len(new_models) == len(models):
                await channel.send_response(
                    ws, req_id, ok=False,
                    error=f"Model '{model_name}' not found",
                    code="NOT_FOUND",
                )
                return

            update_default_models_in_config(new_models)
            await _clear_agent_config_cache(_resolve(agent_client))
            if on_config_saved:
                config_payload = get_config()
                callback_result = on_config_saved(
                    set(),
                    env_updates={},
                    config_payload=config_payload,
                )
                if inspect.isawaitable(callback_result):
                    await callback_result

            await channel.send_response(ws, req_id, ok=True, payload={"model_name": model_name})
        except Exception as exc:  # noqa: BLE001
            logger.warning("[models.remove] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _models_validate(ws, req_id, params, session_id):
        """测试指定模型配置是否可用（复用 config.validate_model 逻辑）。"""
        await _config_validate_model(ws, req_id, params, session_id)

    async def _models_set_active(ws, req_id, params, session_id):
        """设置默认选中的模型（将其移到列表第一位）。"""
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        model_name = str(params.get("model_name") or "").strip()
        if not model_name:
            await channel.send_response(ws, req_id, ok=False, error="model_name is required", code="BAD_REQUEST")
            return

        try:
            models = _get_raw_model_list()
            target_idx = None
            for i, entry in enumerate(models):
                if _entry_model_name(entry) == model_name:
                    target_idx = i
                    break
            if target_idx is None:
                await channel.send_response(
                    ws, req_id, ok=False,
                    error=f"Model '{model_name}' not found",
                    code="NOT_FOUND",
                )
                return
            if target_idx != 0:
                target = models.pop(target_idx)
                models.insert(0, target)
                update_default_models_in_config(models)
                await _clear_agent_config_cache(_resolve(agent_client))

            await channel.send_response(ws, req_id, ok=True, payload={"model_name": model_name})
        except Exception as exc:  # noqa: BLE001
            logger.warning("[models.set_active] %s", exc)
            await channel.send_response(ws, req_id, ok=False, error=str(exc), code="INTERNAL_ERROR")

    async def _channel_get(ws, req_id, params, session_id):
        """返回已注册的 channel 列表."""
        cm = _resolve(channel_manager)
        if cm is not None:
            channels = [{"channel_id": cid} for cid in cm.enabled_channels]
        else:
            channels = []
        await channel.send_response(ws, req_id, ok=True, payload={"channels": channels})

    async def _updater_get_status(ws, req_id, params, session_id):
        service = updater_service or WindowsUpdaterService()
        await channel.send_response(ws, req_id, ok=True, payload=service.get_status())

    async def _updater_check(ws, req_id, params, session_id):
        service = updater_service or WindowsUpdaterService()
        manual = bool((params or {}).get("manual", False)) if isinstance(params, dict) else False
        payload = await asyncio.to_thread(service.check, manual)
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _updater_download(ws, req_id, params, session_id):
        service = updater_service or WindowsUpdaterService()
        payload = service.start_download()
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _updater_get_conf(ws, req_id, params, session_id):
        service = updater_service or WindowsUpdaterService()
        await channel.send_response(ws, req_id, ok=True, payload=service.get_runtime_config())

    async def _updater_set_conf(ws, req_id, params, session_id):
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return

        updates: dict[str, Any] = {}
        if "enabled" in params:
            updates["enabled"] = bool(params.get("enabled"))
        for key in ("repo_owner", "repo_name", "release_api_url", "asset_name_pattern", "sha256_name_pattern"):
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

        service = updater_service or WindowsUpdaterService()
        await channel.send_response(ws, req_id, ok=True, payload=service.get_runtime_config())

    async def _session_list(ws, req_id, params, session_id):
        """返回会话列表,包含完整的会话管理信息。"""
        from jiuwenclaw.gateway.session_index import is_remote_storage

        if is_remote_storage():
            # 正常由 Web 入站转发至 MessageHandler 队列（与 session.create/delete 顺序一致）。
            # 若未注册转发则兜底读索引，避免误返回空列表且打日志便于排查配置/启动顺序问题。
            logger.warning(
                "[session.list] remote 模式执行了 WebChannel 本地 handler（缺少入站转发时）；"
                "将就地读网关索引。",
            )
            from jiuwenclaw.gateway.session_index import list_sessions_page

            sessions, total, limit, offset = list_sessions_page(
                params if isinstance(params, dict) else {},
            )
            await channel.send_response(ws, req_id, ok=True, payload={
                "sessions": sessions,
                "total": total,
                "limit": limit,
                "offset": offset,
            })
            return

        # local 模式：保持现有扫盘逻辑
        limit = 20
        offset = 0
        if isinstance(params, dict):
            raw_limit = params.get("limit")
            if isinstance(raw_limit, int):
                limit = raw_limit
            elif isinstance(raw_limit, str) and raw_limit.strip().isdigit():
                limit = int(raw_limit.strip())

            raw_offset = params.get("offset")
            if isinstance(raw_offset, int):
                offset = raw_offset
            elif isinstance(raw_offset, str) and raw_offset.strip().isdigit():
                offset = int(raw_offset.strip())

        limit = max(1, min(limit, 200))
        offset = max(0, offset)

        from jiuwenclaw.agentserver.session_metadata import get_all_sessions_metadata

        # 使用默认多租户路径（单租户作为多租户的默认特例）
        sessions_root = get_multi_tenant_user_workspace_dir("default", "default") / "agent" / "sessions"

        sessions, total = get_all_sessions_metadata(
            limit=limit, offset=offset, sessions_root=sessions_root
        )

        await channel.send_response(ws, req_id, ok=True, payload={
            "sessions": sessions,
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    async def _session_create(ws, req_id, params, session_id):
        """创建一个新 session（在 agent/sessions 下创建一个新目录）。"""
        if not isinstance(params, dict):
            await channel.send_response(
                ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST",
            )
            return
        session_id_to_create = params.get("session_id")
        if not isinstance(session_id_to_create, str) or not session_id_to_create.strip():
            await channel.send_response(
                ws, req_id, ok=False, error="session_id is required", code="BAD_REQUEST",
            )
            return
        session_id_to_create = session_id_to_create.strip()
        safe_sid = normalize_safe_session_id(session_id_to_create)
        if safe_sid is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="invalid session_id",
                code="BAD_REQUEST",
            )
            return

        from jiuwenclaw.gateway.session_index import is_remote_storage
        if is_remote_storage():
            logger.error(
                "[session.create] remote 模式但执行了本地 handler（转发未生效？）session_id=%s",
                safe_sid,
            )
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="remote session storage is enabled but request was not forwarded to agent",
                code="INTERNAL_CONFIGURATION_ERROR",
            )
            return

        # local 模式：保持现有逻辑
        workspace_session_dir = get_agent_sessions_dir()
        if not workspace_session_dir.exists():
            workspace_session_dir.mkdir(parents=True)
        session_dir = resolve_session_dir_under_root(workspace_session_dir, safe_sid)
        if session_dir is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="invalid session_id",
                code="BAD_REQUEST",
            )
            return
        if session_dir.exists():
            await channel.send_response(
                ws, req_id, ok=False, error="session already exists", code="ALREADY_EXISTS",
            )
            return
        session_dir.mkdir()

        # 初始化会话元数据
        from jiuwenclaw.agentserver.session_metadata import init_session_metadata
        pd_raw = params.get("project_dir")
        init_project_dir = pd_raw.strip() if isinstance(pd_raw, str) else None
        try:
            init_session_metadata(
                session_id=safe_sid,
                channel_id=params.get("channel_id", ""),
                user_id=params.get("user_id", ""),
                title=params.get("title", ""),
                mode=params.get("mode", "unknown"),
                project_dir=init_project_dir,
            )
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(session_dir, ignore_errors=True)
            logger.warning("[session.create] init_session_metadata 失败，已清理目录: %s", exc)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code="INTERNAL_ERROR",
            )
            return

        await channel.send_response(
            ws, req_id, ok=True, payload={"session_id": safe_sid}
        )

    async def _session_delete(ws, req_id, params, session_id):
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
        safe_sid = normalize_safe_session_id(session_id_to_delete)
        if safe_sid is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="invalid session_id",
                code="BAD_REQUEST",
            )
            return

        from jiuwenclaw.gateway.session_index import is_remote_storage
        if is_remote_storage():
            logger.error(
                "[session.delete] remote 模式但执行了本地 handler（转发未生效？）session_id=%s",
                safe_sid,
            )
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="remote session storage is enabled but request was not forwarded to agent",
                code="INTERNAL_CONFIGURATION_ERROR",
            )
            return

        # local 模式：保持现有逻辑
        workspace_session_dir = get_agent_sessions_dir()
        session_dir = resolve_session_dir_under_root(workspace_session_dir, safe_sid)
        if session_dir is None:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="invalid session_id",
                code="BAD_REQUEST",
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
        shutil.rmtree(session_dir)
        await channel.send_response(
            ws, req_id, ok=True, payload={"session_id": safe_sid}
        )

    async def _path_get(ws, req_id, params, session_id):
        """读 browser.chrome_path 并返回给前端（会解析环境变量）。"""
        try:
            config_base = get_config()
        except FileNotFoundError:
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload={"chrome_path": ""},
            )
            return

        if not isinstance(config_base, dict):
            config_base = {}

        config = _resolve_env_vars(config_base)
        browser_cfg = config.get("browser", {}) if isinstance(config, dict) else {}
        chrome_path = ""
        if isinstance(browser_cfg, dict):
            value = browser_cfg.get("chrome_path", "")
            if isinstance(value, str):
                chrome_path = value

        await channel.send_response(ws, req_id, ok=True, payload={"chrome_path": chrome_path})

    async def _path_set(ws, req_id, params, session_id):
        """更新 browser.chrome_path 并写回 config。"""
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return

        chrome_path = params.get("chrome_path")
        if not isinstance(chrome_path, str):
            await channel.send_response(ws, req_id, ok=False, error="chrome_path must be string", code="BAD_REQUEST")
            return
        chrome_path = chrome_path.strip()

        try:
            update_browser_in_config({"chrome_path": chrome_path})
            await _clear_agent_config_cache(_resolve(agent_client))
        except Exception as e:  # noqa: BLE001
            logger.warning("[path.set] 写回 config.yaml 失败: %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")
            return

        await channel.send_response(ws, req_id, ok=True, payload={"chrome_path": chrome_path})

    async def _memory_compute(ws, req_id, params, session_id):

        process = psutil.Process()
        rss_bytes = process.memory_info().rss  # 物理内存
        rss_mb = rss_bytes / (1024 * 1024)

        mem = psutil.virtual_memory()
        total_mb = mem.total / (1024 * 1024)
        available_mb = mem.available / (1024 * 1024)
        used_percent = mem.percent

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

    async def _skilldev_cancel(ws, req_id, params, session_id):
        sid = session_id
        task_id = None
        if isinstance(params, dict):
            raw_sid = params.get("session_id")
            if isinstance(raw_sid, str) and raw_sid.strip():
                sid = raw_sid.strip()
            raw_task = params.get("task_id")
            if isinstance(raw_task, str) and raw_task.strip():
                task_id = raw_task.strip()
        payload: dict = {"accepted": True, "session_id": sid}
        if task_id:
            payload["task_id"] = task_id
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _skilldev_restore(ws, req_id, params, session_id):
        ac = _resolve(agent_client)
        if ac is None or (
            hasattr(ac, "server_ready") and not getattr(ac, "server_ready", False)
        ):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="agent server is not ready",
                code="AGENT_NOT_READY",
            )
            return

        restore_params = dict(params) if isinstance(params, dict) else {}
        task_id = str(restore_params.get("task_id") or session_id or "").strip()
        if not task_id:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="task_id is required",
                code="BAD_REQUEST",
            )
            return
        restore_params["task_id"] = task_id

        try:
            import secrets
            import time

            from jiuwenclaw.e2a.gateway_normalize import e2a_from_agent_fields
            from jiuwenclaw.schema.message import ReqMethod

            def _open_restore_stream():
                stream_request_id = (
                    f"skilldev-restore-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"
                )
                env = e2a_from_agent_fields(
                    request_id=stream_request_id,
                    channel_id=channel.channel_id,
                    session_id=task_id,
                    req_method=ReqMethod.SKILLDEV_RESTORE,
                    params=restore_params,
                    is_stream=True,
                    timestamp=time.time(),
                )
                return ac.send_request_stream(env)

            payload = await decode_restore_payload_from_stream_with_retry(
                _open_restore_stream,
            )
        except RestoreChunkDecodeError as exc:
            logger.info("[skilldev.restore] stream decode failed: code=%s error=%s", exc.code, exc)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code=exc.code,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("[skilldev.restore] forward to agent failed: %s", exc)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code="INTERNAL_ERROR",
            )
            return

        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _skilldev_file_read(ws, req_id, params, session_id):
        ac = _resolve(agent_client)
        if ac is None or (
            hasattr(ac, "server_ready") and not getattr(ac, "server_ready", False)
        ):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="agent server is not ready",
                code="AGENT_NOT_READY",
            )
            return

        read_params = dict(params) if isinstance(params, dict) else {}
        task_id = str(read_params.get("task_id") or session_id or "").strip()
        file_path = str(read_params.get("path") or "").strip()
        if not task_id:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="task_id is required",
                code="BAD_REQUEST",
            )
            return
        if not file_path:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="path is required",
                code="BAD_REQUEST",
            )
            return
        read_params["task_id"] = task_id
        read_params["path"] = file_path

        try:
            import secrets
            import time

            from jiuwenclaw.e2a.gateway_normalize import e2a_from_agent_fields
            from jiuwenclaw.schema.message import ReqMethod

            def _open_file_read_stream():
                stream_request_id = (
                    f"skilldev-file-read-{int(time.time() * 1000):x}-{secrets.token_hex(3)}"
                )
                env = e2a_from_agent_fields(
                    request_id=stream_request_id,
                    channel_id=channel.channel_id,
                    session_id=task_id,
                    req_method=ReqMethod.SKILLDEV_FILE_READ,
                    params=read_params,
                    is_stream=True,
                    timestamp=time.time(),
                )
                return ac.send_request_stream(env)

            payload = await decode_file_read_payload_from_stream_with_retry(
                _open_file_read_stream,
            )
        except FileReadChunkDecodeError as exc:
            logger.info("[skilldev.file.read] stream decode failed: code=%s error=%s", exc.code, exc)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code=exc.code,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("[skilldev.file.read] forward to agent failed: %s", exc)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code="INTERNAL_ERROR",
            )
            return

        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _skilldev_file_write(ws, req_id, params, session_id):
        ac = _resolve(agent_client)
        if ac is None or (
            hasattr(ac, "server_ready") and not getattr(ac, "server_ready", False)
        ):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="agent server is not ready",
                code="AGENT_NOT_READY",
            )
            return

        write_params = dict(params) if isinstance(params, dict) else {}
        task_id = str(write_params.get("task_id") or session_id or "").strip()
        file_path = str(write_params.get("path") or "").strip()
        content = write_params.pop("content", None)
        if not task_id or not file_path:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="task_id and path are required",
                code="BAD_REQUEST",
            )
            return
        if not isinstance(content, str):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error="content must be a string",
                code="BAD_REQUEST",
            )
            return
        write_params["task_id"] = task_id
        write_params["path"] = file_path

        try:
            from jiuwenclaw.e2a.gateway_normalize import e2a_from_agent_fields
            from jiuwenclaw.schema.message import ReqMethod

            async def _send_write_request(
                phase_params: dict[str, Any], phase: str, attempt: int
            ):
                request_id = (
                    f"skilldev-file-write-{phase}-{attempt}-"
                    f"{int(time.time() * 1000):x}-{secrets.token_hex(3)}"
                )
                env = e2a_from_agent_fields(
                    request_id=request_id,
                    channel_id=channel.channel_id,
                    session_id=task_id,
                    req_method=ReqMethod.SKILLDEV_FILE_WRITE,
                    params=phase_params,
                    is_stream=False,
                    timestamp=time.time(),
                )
                return await ac.send_request(env)

            response = await send_file_write_with_chunks(
                base_params=write_params,
                content=content,
                write_id=secrets.token_hex(16),
                send_request=_send_write_request,
            )
        except FileWriteChunkError as exc:
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code="BAD_REQUEST",
            )
            return
        except FileWriteTransportError as exc:
            logger.warning("[skilldev.file.write] transport failed: %s", exc)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code="BAD_GATEWAY",
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("[skilldev.file.write] forward to agent failed: %s", exc)
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(exc),
                code="INTERNAL_ERROR",
            )
            return

        payload = dict(response.payload) if isinstance(response.payload, dict) else {}
        if not response.ok or not payload.get("ok", False):
            await channel.send_response(
                ws,
                req_id,
                ok=False,
                error=str(payload.get("error") or "request failed"),
                code=str(payload.get("code") or "BAD_REQUEST"),
            )
            return
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _chat_user_answer(ws, req_id, params, session_id):
        payload = {"accepted": True, "session_id": session_id}
        request_id = params.get("request_id") if isinstance(params, dict) else None
        if isinstance(request_id, str) and request_id:
            payload["request_id"] = request_id
        await channel.send_response(ws, req_id, ok=True, payload=payload)

    async def _skilldev_user_answer(ws, req_id, params, session_id):
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
            await hb.set_heartbeat_conf(every=every, target=target, active_hours=active_hours)
            payload = dict(hb.get_heartbeat_conf())
            try:
                update_heartbeat_in_config(payload)
                await _clear_agent_config_cache(_resolve(agent_client))
            except Exception as e:  # noqa: BLE001
                logger.warning("[heartbeat.set_conf] 写回 config.yaml 失败: %s", e)
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except ValueError as e:
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="BAD_REQUEST")
        except Exception as e:
            logger.exception("[heartbeat.set_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _heartbeat_get_path(ws, req_id, params, session_id):
        """返回 HEARTBEAT.md 文件路径。"""
        from jiuwenclaw.utils import get_deepagent_heartbeat_path, get_agent_root_dir

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

    async def _channel_feishu_get_conf(ws, req_id, params, session_id):
        """返回 FeishuChannel 的当前配置（由 ChannelManager 管理）。"""
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
            conf = cm.get_conf("feishu")
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.feishu.get_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_feishu_set_conf(ws, req_id, params, session_id):
        """更新 FeishuChannel 的配置，并按新配置重新实例化通道。"""
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
            await cm.set_conf("feishu", params)
            conf = cm.get_conf("feishu")
            try:
                update_channel_in_config("feishu", conf)
                await _clear_agent_config_cache(_resolve(agent_client))
            except Exception as e:  # noqa: BLE001
                logger.warning("[channel.feishu.set_conf] 写回 config.yaml 失败: %s", e)
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.feishu.set_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_xiaoyi_get_conf(ws, req_id, params, session_id):
        """返回 XiaoyiChannel 的当前配置（由 ChannelManager 管理）。"""
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
            conf = cm.get_conf("xiaoyi")
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.xiaoyi.get_conf] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _channel_xiaoyi_set_conf(ws, req_id, params, session_id):
        """更新 XiaoyiChannel 的配置，并按新配置重新实例化通道。"""
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
            await cm.set_conf("xiaoyi", params)
            conf = cm.get_conf("xiaoyi")
            try:
                update_channel_in_config("xiaoyi", conf)
                await _clear_agent_config_cache(_resolve(agent_client))
            except Exception as e:  # noqa: BLE001
                logger.warning("[channel.xiaoyi.set_conf] 写回 config.yaml 失败: %s", e)
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
        except Exception as e:  # noqa: BLE001
            logger.exception("[channel.xiaoyi.set_conf] %s", e)
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
            try:
                update_channel_in_config("dingtalk", conf)
                await _clear_agent_config_cache(_resolve(agent_client))
            except Exception as e:  # noqa: BLE001
                logger.warning("[channel.dingtalk.set_conf] 写回 config.yaml 失败: %s", e)
            await channel.send_response(ws, req_id, ok=True, payload={"config": conf})
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
        from jiuwenclaw.channel.wechat_channel import snapshot_wechat_login_ui_state

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
            from jiuwenclaw.channel.wechat_channel import clear_wechat_bound_session, reset_wechat_login_ui_state

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

    async def _cron_job_list(ws, req_id, params, session_id):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        jobs = await cc.list_jobs()
        await channel.send_response(ws, req_id, ok=True, payload={"jobs": jobs})

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

    async def _cron_job_create(ws, req_id, params, session_id):
        cc = _get_cron()
        if cc is None:
            await channel.send_response(ws, req_id, ok=False, error="cron not available", code="INTERNAL_ERROR")
            return
        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        try:
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
            run_id = await cc.run_now(job_id)
            await channel.send_response(ws, req_id, ok=True, payload={"run_id": run_id})
        except KeyError:
            await channel.send_response(ws, req_id, ok=False, error="job not found", code="NOT_FOUND")
        except Exception as e:  # noqa: BLE001
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    channel.register_method("config.get", _config_get)
    channel.register_method("config.set", _config_set)
    channel.register_method("config.validate_model", _config_validate_model)
    channel.register_method("models.list", _models_list)
    channel.register_method("models.save", _models_save)
    channel.register_method("models.remove", _models_remove)
    channel.register_method("models.validate", _models_validate)
    channel.register_method("models.set_active", _models_set_active)
    channel.register_method("channel.get", _channel_get)

    channel.register_method("session.list", _session_list)
    channel.register_method("session.create", _session_create)
    channel.register_method("session.delete", _session_delete)

    channel.register_method("path.get", _path_get)
    channel.register_method("path.set", _path_set)

    channel.register_method("memory.compute", _memory_compute)

    channel.register_method("chat.send", _chat_send)
    channel.register_method("chat.resume", _chat_resume)
    channel.register_method("chat.interrupt", _chat_interrupt)
    channel.register_method("skilldev.cancel", _skilldev_cancel)
    channel.register_method("skilldev.restore", _skilldev_restore)
    channel.register_method("skilldev.file.read", _skilldev_file_read)
    channel.register_method("skilldev.file.write", _skilldev_file_write)
    channel.register_method("chat.user_answer", _chat_user_answer)
    channel.register_method("skilldev.user_answer", _skilldev_user_answer)
    channel.register_method("history.get", _history_get)
    channel.register_method("locale.get_conf", _locale_get_conf)
    channel.register_method("locale.set_conf", _locale_set_conf)
    channel.register_method("updater.get_status", _updater_get_status)
    channel.register_method("updater.check", _updater_check)
    channel.register_method("updater.download", _updater_download)
    channel.register_method("updater.get_conf", _updater_get_conf)
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
    channel.register_method("channel.wecom.get_conf", _channel_wecom_get_conf)
    channel.register_method("channel.wecom.set_conf", _channel_wecom_set_conf)
    channel.register_method("channel.wechat.get_conf", _channel_wechat_get_conf)
    channel.register_method("channel.wechat.set_conf", _channel_wechat_set_conf)
    channel.register_method("channel.wechat.get_login_ui", _channel_wechat_get_login_ui)
    channel.register_method("channel.wechat.unbind", _channel_wechat_unbind)
    channel.register_method("cron.job.list", _cron_job_list)
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
        from jiuwenclaw.config import get_permissions_owner_scopes

        try:
            payload = get_permissions_owner_scopes()
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except Exception as e:
            logger.exception("[permissions.owner_scopes.get] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _permissions_owner_scopes_set(ws, req_id, params, session_id):
        from jiuwenclaw.config import update_permissions_owner_scopes_in_config
        from jiuwenclaw.agentserver.permissions.core import get_permission_engine

        if not isinstance(params, dict):
            await channel.send_response(ws, req_id, ok=False, error="params must be object", code="BAD_REQUEST")
            return
        try:
            owner_scopes = params.get("owner_scopes", {})
            deny_guidance = params.get("deny_guidance_message")
            update_permissions_owner_scopes_in_config(owner_scopes, deny_guidance)
            try:
                perm_cfg = get_config().get("permissions", {})
                get_permission_engine().update_config(perm_cfg)
            except Exception as e:
                logger.warning("[permissions.owner_scopes.set] Failed to hot reload permission engine: %s", e)
            await channel.send_response(ws, req_id, ok=True, payload={"ok": True})
        except Exception as e:
            logger.exception("[permissions.owner_scopes.set] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    channel.register_method("permissions.owner_scopes.get", _permissions_owner_scopes_get)
    channel.register_method("permissions.owner_scopes.set", _permissions_owner_scopes_set)

    async def _forward_permissions_to_agent(ws, req_id, params, session_id, *, req_method):
        """permissions.*：优先经 E2A 转发到 AgentServer；Agent 未就绪时本地执行（与 config_rpc 同源）。"""
        from jiuwenclaw.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenclaw.schema.agent import AgentRequest
        from jiuwenclaw.schema.message import ReqMethod

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
            from jiuwenclaw.agentserver.permissions.config_rpc import dispatch_permissions_config_request

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

    from jiuwenclaw.schema.message import ReqMethod as _PermReq

    def _register_perm(method_name: str, rm: Any) -> None:
        async def _handler(ws, req_id, params, session_id):
            await _forward_permissions_to_agent(ws, req_id, params, session_id, req_method=rm)

        channel.register_method(method_name, _handler)

    _register_perm("permissions.tools.get", _PermReq.PERMISSIONS_TOOLS_GET)
    _register_perm("permissions.enabled.get", _PermReq.PERMISSIONS_ENABLED_GET)
    _register_perm("permissions.enabled.set", _PermReq.PERMISSIONS_ENABLED_SET)
    _register_perm("permissions.tools.set", _PermReq.PERMISSIONS_TOOLS_SET)
    _register_perm("permissions.tools.update", _PermReq.PERMISSIONS_TOOLS_UPDATE)
    _register_perm("permissions.tools.delete", _PermReq.PERMISSIONS_TOOLS_DELETE)
    _register_perm("permissions.rules.get", _PermReq.PERMISSIONS_RULES_GET)
    _register_perm("permissions.rules.create", _PermReq.PERMISSIONS_RULES_CREATE)
    _register_perm("permissions.rules.update", _PermReq.PERMISSIONS_RULES_UPDATE)
    _register_perm("permissions.rules.delete", _PermReq.PERMISSIONS_RULES_DELETE)
    _register_perm("permissions.approval_overrides.get", _PermReq.PERMISSIONS_APPROVAL_OVERRIDES_GET)
    _register_perm("permissions.approval_overrides.delete", _PermReq.PERMISSIONS_APPROVAL_OVERRIDES_DELETE)
    _register_perm(
        "permissions.file_guard.workspace.rw_enabled.get",
        _PermReq.PERMISSIONS_WORKSPACE_ENABLE_GET,
    )
    _register_perm(
        "permissions.file_guard.workspace.rw_enabled.set",
        _PermReq.PERMISSIONS_WORKSPACE_ENABLE_SET,
    )

    async def _memory_forbidden_get(ws, req_id, params, session_id):
        try:
            cfg = get_config() or {}
            payload = cfg.get("memory", {}).get("forbidden_memory_definition", {})
            await channel.send_response(ws, req_id, ok=True, payload=payload)
        except Exception as e:
            logger.exception("[memory.forbidden.get] %s", e)
            await channel.send_response(ws, req_id, ok=False, error=str(e), code="INTERNAL_ERROR")

    async def _memory_forbidden_set(ws, req_id, params, session_id):
        from jiuwenclaw.config import update_memory_forbidden_in_config
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
