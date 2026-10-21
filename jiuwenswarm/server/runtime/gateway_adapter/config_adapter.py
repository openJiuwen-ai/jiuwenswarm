"""Configuration-panel adapter.

The configuration panel is a user-state entrypoint.  It must run in the
AgentServer process so ``get_config*`` and ``get_env_file`` resolve the
AgentServer's injected data directory.  The panel implementation is retained
in one place for now; this adapter supplies its transport boundary and turns
its response into an E2A response.
"""

from __future__ import annotations

import inspect
import logging
import sys
from types import SimpleNamespace
from typing import Any

from jiuwenswarm.common.config import resolve_env_vars
from jiuwenswarm.common.e2a.constants import E2A_AUTH_SESSION_PARAM_KEY
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter, build_error_response

logger = logging.getLogger(__name__)


def _append_zen_free_models(payload: dict[str, Any]) -> None:
    """Add AgentServer's Zen cache to a models.list payload."""
    models = payload.get("models")
    if not isinstance(models, list):
        return
    try:
        from jiuwenswarm.server.runtime.opencode_zen import append_zen_free_models

        append_zen_free_models(models)
    except Exception:  # noqa: BLE001
        logger.warning("[ConfigAdapter] append Zen free models failed", exc_info=True)


async def _warm_zen_after_free_model_toggle(payload: dict[str, Any]) -> None:
    """Warm this process when a config write toggled free models."""
    updated = payload.get("updated")
    if not isinstance(updated, list) or "enable_free_models" not in updated:
        return
    try:
        from jiuwenswarm.server.runtime.opencode_zen import warm_zen_free_models

        await warm_zen_free_models(reason="config-toggle")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ConfigAdapter] warm_zen_free_models failed: %s", exc)


def _resolve_browser_path(browser: dict[str, Any]) -> str:
    """Return the current platform's configured managed-browser binary."""
    resolved_browser = resolve_env_vars(browser)
    if not isinstance(resolved_browser, dict):
        return ""
    chrome_path = resolved_browser.get("chrome_path", "")
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


class _ConfigRpcChannel:
    """Minimal in-process channel used by the established config panel RPCs."""

    def __init__(self, channel_id: str) -> None:
        self.channel_id = channel_id or "web"
        self.methods: dict[str, Any] = {}
        self.response: dict[str, Any] | None = None

    def register_method(self, name: str, handler: Any) -> None:
        self.methods[name] = handler

    def register_local_handler(self, _path: str, name: str, handler: Any) -> None:
        """Support reusing TUI's established local RPC handlers."""
        self.methods[name] = handler

    @staticmethod
    def on_connect(_handler: Any) -> None:
        # Config RPCs do not require a connection lifecycle callback.
        return None

    async def send_response(
        self, _ws: Any, _req_id: str, *, ok: bool, payload: Any = None,
        error: str | None = None, code: str | None = None,
    ) -> None:
        self.response = {
            "ok": bool(ok), "payload": payload if isinstance(payload, dict) else {},
            "error": error, "code": code,
        }


async def reload_runtime_after_config_save(
    agent_manager: Any,
    _updated_keys: Any = None,
    *,
    env_updates: dict[str, Any] | None = None,
    config_payload: dict[str, Any] | None = None,
    reload_options: dict[str, Any] | None = None,
) -> bool:
    """Publish a saved config to every agent in this process before acknowledging it.

    Permission changes share one config file across channels, so they are not
    narrowed to the channel that submitted the panel request.
    """
    options = reload_options if isinstance(reload_options, dict) else {}
    scopes = {
        str(scope)
        for scope in (options.get("reload_scopes") or [])
        if isinstance(scope, str) and scope
    }
    target_channel = str(options.get("target_channel_id") or "").strip() or None
    if "permissions" in scopes:
        target_channel = None
    await agent_manager.reload_agents_config(
        config_payload if isinstance(config_payload, dict) else None,
        env_updates if isinstance(env_updates, dict) else None,
        target_channel_id=target_channel,
        reload_scopes=scopes or None,
    )
    return True


class ConfigAdapter(GatewayAdapter):
    """Execute config-panel requests in the current AgentServer directory."""

    def __init__(self, on_config_saved: Any = None) -> None:
        self._on_config_saved = on_config_saved

    methods = frozenset({
        ReqMethod.CONFIG_GET.value,
        ReqMethod.CONFIG_MOA_STATUS.value,
        ReqMethod.CONFIG_SET.value,
        ReqMethod.CONFIG_SAVE_ALL.value,
        ReqMethod.CONFIG_VALIDATE_MODEL.value,
        ReqMethod.MODELS_LIST.value,
        ReqMethod.MODELS_ZEN_ENTRIES.value,
        ReqMethod.MODELS_ZEN_WARM.value,
        ReqMethod.MODELS_GET.value,
        ReqMethod.MODELS_REPLACE_ALL.value,
        ReqMethod.MODELS_VALIDATE.value,
        ReqMethod.MODELS_UPSERT.value,
        ReqMethod.MODELS_DELETE.value,
        ReqMethod.MODELS_REFERENCES.value,
        ReqMethod.MODEL_GROUPS_LIST.value,
        ReqMethod.MODEL_GROUPS_UPSERT.value,
        ReqMethod.MODEL_GROUPS_DELETE.value,
        ReqMethod.SESSION_SELECTION_SET.value,
        ReqMethod.LOCALE_GET_CONF.value,
        ReqMethod.LOCALE_SET_CONF.value,
        ReqMethod.COMMAND_MODEL.value,
        ReqMethod.PATH_GET.value,
        ReqMethod.PATH_SET.value,
        ReqMethod.PERMISSIONS_OWNER_SCOPES_GET.value,
        ReqMethod.PERMISSIONS_OWNER_SCOPES_SET.value,
        ReqMethod.MEMORY_FORBIDDEN_GET.value,
        ReqMethod.MEMORY_FORBIDDEN_SET.value,
    })

    async def handle(self, request: AgentRequest) -> AgentResponse:
        if request.req_method == ReqMethod.CONFIG_MOA_STATUS:
            from jiuwenswarm.common.config import get_config
            from jiuwenswarm.server.runtime.moa_runtime import moa_status

            return AgentResponse(
                request_id=request.request_id, channel_id=request.channel_id,
                ok=True,
                payload=moa_status(get_config(), (request.params or {}).get("model_selection")),
                metadata=request.metadata,
            )

        if request.req_method == ReqMethod.MODELS_ZEN_ENTRIES:
            from jiuwenswarm.server.runtime.opencode_zen import public_zen_free_model_rows

            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"models": public_zen_free_model_rows()},
                metadata=request.metadata,
            )
        if request.req_method == ReqMethod.MODELS_ZEN_WARM:
            from jiuwenswarm.server.runtime.opencode_zen import warm_zen_free_models

            await warm_zen_free_models(reason="config-toggle")
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"ok": True},
                metadata=request.metadata,
            )

        if request.req_method in {ReqMethod.LOCALE_GET_CONF, ReqMethod.LOCALE_SET_CONF}:
            from jiuwenswarm.common.config import (
                get_config,
                update_preferred_language_in_config,
            )

            if request.req_method == ReqMethod.LOCALE_GET_CONF:
                lang = str(get_config().get("preferred_language") or "zh").strip().lower()
                if lang not in {"zh", "en"}:
                    lang = "zh"
                return AgentResponse(
                    request_id=request.request_id, channel_id=request.channel_id,
                    ok=True, payload={"preferred_language": lang}, metadata=request.metadata,
                )
            raw_lang = (request.params or {}).get("preferred_language")
            if not isinstance(raw_lang, str) or raw_lang.strip().lower() not in {"zh", "en"}:
                return build_error_response(
                    request, "preferred_language must be zh or en", code="BAD_REQUEST"
                )
            lang = raw_lang.strip().lower()
            update_preferred_language_in_config(lang)
            return AgentResponse(
                request_id=request.request_id, channel_id=request.channel_id,
                ok=True, payload={"preferred_language": lang}, metadata=request.metadata,
            )

        if request.req_method == ReqMethod.COMMAND_MODEL:
            return await self._handle_tui_command_model(request)

        if request.req_method in {ReqMethod.PATH_GET, ReqMethod.PATH_SET}:
            return await self._handle_browser_config(request)

        if request.req_method in {
            ReqMethod.PERMISSIONS_OWNER_SCOPES_GET,
            ReqMethod.PERMISSIONS_OWNER_SCOPES_SET,
        }:
            return await self._handle_permissions_owner_scopes(request)

        if request.req_method in {ReqMethod.MEMORY_FORBIDDEN_GET, ReqMethod.MEMORY_FORBIDDEN_SET}:
            return await self._handle_memory_forbidden(request)

        # Keep the mature panel serialization/validation implementation shared
        # while making its execution context (and therefore config directory)
        # unambiguously AgentServer-owned. The implementation lives in
        # common.config_panel (channel-specific modules; the TUI contract
        # differs from the Web one, so each side registers its own handlers).
        channel = _ConfigRpcChannel(request.channel_id)
        if request.channel_id == "tui":
            # TUI has a distinct config schema (notably Auto-Harness fields).
            from jiuwenswarm.common.config_panel.tui_models_handlers import (
                register_tui_config_handlers,
            )

            # A4g review P2：TUI 通道同样接 server 侧 in-process reload（B1'
            # 既有缺口——原先只给 web 通道传回调；统一 E2A 后 legacy TUI 的
            # config.set 也走此路径，热重载语义必须补齐）。
            register_tui_config_handlers(
                channel,
                on_config_saved=self._on_config_saved,
                agent_client=None,
            )
        else:
            from jiuwenswarm.common.config_panel.config_set_handlers import (
                register_config_set_handlers,
            )
            from jiuwenswarm.common.config_panel.models_handlers import (
                register_models_handlers,
            )

            register_config_set_handlers(
                channel,
                on_config_saved=self._on_config_saved,
            )
            # models/model_groups 域的写操作同样触发 server 侧 in-process reload
            # （models.replace_all / upsert / delete 改动模型配置后 agent 需热生效；
            # 原先只给 config.set/save_all 接回调，E2A 路径漏掉了模型域）
            register_models_handlers(
                channel,
                on_config_saved=self._on_config_saved,
                user_id=request.user_id,
            )
        handler = channel.methods.get(request.req_method.value)
        if handler is None:
            return build_error_response(request, "unsupported config method", code="BAD_REQUEST")
        # 登录会话 id 由 Gateway 从受认证连接属性注入（键约定见
        # e2a.constants.E2A_AUTH_SESSION_PARAM_KEY）；本地构造 ws 上下文还原，
        # models.list 据此合并登录送的免费模型（common handler 读 ws 属性）。
        handler_params = dict(request.params or {})
        auth_session = str(handler_params.pop(E2A_AUTH_SESSION_PARAM_KEY, "") or "").strip()
        handler_ws = SimpleNamespace(_jiuwen_auth_session=auth_session)
        await handler(handler_ws, request.request_id, handler_params, request.session_id)
        response = channel.response
        if response is None:
            return build_error_response(request, "config handler produced no response")
        metadata = dict(request.metadata or {})
        if response["ok"] and request.req_method in {
            ReqMethod.CONFIG_SET,
            ReqMethod.CONFIG_SAVE_ALL,
            ReqMethod.MODELS_REPLACE_ALL,
            ReqMethod.MODELS_UPSERT,
            ReqMethod.MODELS_DELETE,
            ReqMethod.MODEL_GROUPS_UPSERT,
            ReqMethod.MODEL_GROUPS_DELETE,
        }:
            metadata["config_changed"] = True
        payload = dict(response["payload"])
        if response["ok"] and request.req_method == ReqMethod.MODELS_LIST:
            _append_zen_free_models(payload)
        if response["ok"] and request.req_method in {
            ReqMethod.CONFIG_SET,
            ReqMethod.CONFIG_SAVE_ALL,
        }:
            await _warm_zen_after_free_model_toggle(payload)
        if not response["ok"]:
            payload.setdefault("error", response["error"] or "config request failed")
            payload.setdefault("code", response["code"] or "INTERNAL_ERROR")
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=response["ok"],
            payload=payload,
            metadata=metadata,
        )

    async def _handle_browser_config(self, request: AgentRequest) -> AgentResponse:
        from jiuwenswarm.agents.harness.common.browser_config import (
            browser_decision_state,
            browser_decision_updates,
        )
        from jiuwenswarm.common.config import (
            get_config,
            update_browser_decision_in_config,
            update_browser_in_config,
        )

        config = get_config() or {}
        if request.req_method == ReqMethod.PATH_GET:
            browser = config.get("browser") or {}
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "chrome_path": _resolve_browser_path(browser) if isinstance(browser, dict) else "",
                    "headless": browser.get("headless") if isinstance(browser.get("headless"), bool) else True,
                    **browser_decision_state(config),
                },
                metadata=request.metadata,
            )
        params = request.params if isinstance(request.params, dict) else {}
        chrome_path = params.get("chrome_path")
        if not isinstance(chrome_path, str):
            return build_error_response(request, "chrome_path must be string", code="BAD_REQUEST")
        chrome_path = chrome_path.strip()

        headless = params.get("headless", True)
        if not isinstance(headless, bool):
            headless = True
        # Older clients omit the decision fields; the saved values then stay untouched.
        decision_updates, error = browser_decision_updates(
            params.get("decision_mode"), params.get("decision_provider"), config,
            model=params.get("decision_model"), api_base=params.get("decision_api_base"),
        )
        if error:
            return build_error_response(request, error, code="BAD_REQUEST")
        update_browser_in_config({"chrome_path": chrome_path, "headless": headless})
        if decision_updates:
            update_browser_decision_in_config(decision_updates)
        metadata = dict(request.metadata or {})
        metadata["config_changed"] = True
        metadata["browser_runtime_restart"] = True
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={
                "chrome_path": chrome_path,
                "headless": headless,
                **browser_decision_state(get_config() or {}),
            },
            metadata=metadata,
        )

    async def _handle_permissions_owner_scopes(self, request: AgentRequest) -> AgentResponse:
        from jiuwenswarm.common.config import (
            get_permissions_owner_scopes,
            update_permissions_owner_scopes_in_config,
        )

        if request.req_method == ReqMethod.PERMISSIONS_OWNER_SCOPES_GET:
            payload = get_permissions_owner_scopes()
        else:
            params = request.params if isinstance(request.params, dict) else None
            if params is None:
                return build_error_response(request, "params must be object", code="BAD_REQUEST")
            update_permissions_owner_scopes_in_config(
                params.get("owner_scopes", {}), params.get("deny_guidance_message")
            )
            # A5：写入后触发 server 侧 in-process reload（对齐原 gateway 本地
            # apply_config_change_set 的热更新语义；reload_scopes 与其一致）。
            applied_without_restart = True
            if self._on_config_saved is not None:
                from jiuwenswarm.common.config import get_config

                result = self._on_config_saved(
                    ["permissions"],
                    env_updates={},
                    config_payload=get_config() or {},
                    reload_options={"reload_scopes": ["permissions"]},
                )
                if inspect.isawaitable(result):
                    applied_without_restart = bool(await result)
                else:
                    applied_without_restart = bool(result)
            payload = {"ok": True, "applied_without_restart": applied_without_restart}
        metadata = dict(request.metadata or {})
        if request.req_method == ReqMethod.PERMISSIONS_OWNER_SCOPES_SET:
            metadata["config_changed"] = True
        return AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                             ok=True, payload=payload, metadata=metadata)

    async def _handle_memory_forbidden(self, request: AgentRequest) -> AgentResponse:
        from jiuwenswarm.common.config import get_config, update_memory_forbidden_in_config

        if request.req_method == ReqMethod.MEMORY_FORBIDDEN_GET:
            payload = ((get_config() or {}).get("memory") or {}).get("forbidden_memory_definition", {})
        else:
            params = request.params if isinstance(request.params, dict) else None
            if params is None:
                return build_error_response(request, "params must be object", code="BAD_REQUEST")
            update_memory_forbidden_in_config(params)
            payload = {"ok": True}
        metadata = dict(request.metadata or {})
        if request.req_method == ReqMethod.MEMORY_FORBIDDEN_SET:
            metadata["config_changed"] = True
        return AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                             ok=True, payload=payload, metadata=metadata)

    async def _handle_tui_command_model(self, request: AgentRequest) -> AgentResponse:
        """Run the complete legacy TUI model command in this user directory."""
        from jiuwenswarm.common.config_panel.tui_models_handlers import (
            command_model_handler,
        )

        class _ReloadNoopClient:
            server_ready = True

            async def send_request(self, _envelope: Any) -> Any:
                # AgentWebSocketServer reloads the config after the adapter
                # returns, based on the metadata set below.
                return type("Response", (), {"ok": True, "payload": {}})()

        channel = _ConfigRpcChannel(request.channel_id or "tui")
        await command_model_handler(
            channel,
            object(),
            request.request_id,
            request.params or {},
            request.session_id,
            agent_client=_ReloadNoopClient(),
            user_id=request.user_id,
        )
        response = channel.response
        if response is None:
            return build_error_response(request, "command.model produced no response")
        payload = dict(response["payload"])
        if not response["ok"]:
            payload.setdefault("error", response["error"] or "command.model failed")
            payload.setdefault("code", response["code"] or "INTERNAL_ERROR")
        metadata = dict(request.metadata or {})
        if response["ok"] and str(payload.get("type") or "").strip() in {
            "model_added", "model_updated", "model_deleted", "switched",
        }:
            metadata["config_changed"] = True
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=response["ok"],
            payload=payload,
            metadata=metadata,
        )
