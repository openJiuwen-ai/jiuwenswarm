"""Configuration-panel adapter.

The configuration panel is a user-state entrypoint.  It must run in the
AgentServer process so ``get_config*`` and ``get_env_file`` resolve the
AgentServer's injected data directory.  The panel implementation is retained
in one place for now; this adapter supplies its transport boundary and turns
its response into an E2A response.
"""

from __future__ import annotations

import sys
from typing import Any

from jiuwenswarm.common.config import resolve_env_vars
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter, build_error_response


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


class ConfigAdapter(GatewayAdapter):
    """Execute config-panel requests in the current AgentServer directory."""

    methods = frozenset({
        ReqMethod.CONFIG_GET.value,
        ReqMethod.CONFIG_SET.value,
        ReqMethod.CONFIG_SAVE_ALL.value,
        ReqMethod.CONFIG_VALIDATE_MODEL.value,
        ReqMethod.MODELS_LIST.value,
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
        # unambiguously AgentServer-owned.
        channel = _ConfigRpcChannel(request.channel_id)
        if request.channel_id == "tui":
            # TUI has a distinct config schema (notably Auto-Harness fields).
            # Reuse its mature local handler in the AgentServer process instead
            # of silently treating a TUI request as a Web config request.
            from jiuwenswarm.gateway.channel_manager.tui.tui_connect import (
                CliHandlersBindParams,
                register_cli_handlers,
            )

            register_cli_handlers(CliHandlersBindParams(channel=channel, force_local_config=True))
        else:
            from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
                WebHandlersBindParams,
                _register_web_handlers,
            )

            _register_web_handlers(WebHandlersBindParams(channel=channel))
        handler = channel.methods.get(request.req_method.value)
        if handler is None:
            return build_error_response(request, "unsupported config method", code="BAD_REQUEST")
        await handler(object(), request.request_id, request.params or {}, request.session_id)
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
        from jiuwenswarm.common.config import get_config, update_browser_in_config

        if request.req_method == ReqMethod.PATH_GET:
            browser = (get_config() or {}).get("browser") or {}
            browser_type = "auto"
            if isinstance(browser, dict):
                raw_type = browser.get("browser_type", "auto")
                if isinstance(raw_type, str) and raw_type.strip():
                    normalized = raw_type.strip().lower()
                    if normalized in {"chrome", "google-chrome", "google_chrome"}:
                        browser_type = "chrome"
                    elif normalized in {"msedge", "edge", "microsoft-edge", "microsoft_edge"}:
                        browser_type = "msedge"
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={
                    "chrome_path": _resolve_browser_path(browser) if isinstance(browser, dict) else "",
                    "browser_type": browser_type,
                    "headless": browser.get("headless") if isinstance(browser.get("headless"), bool) else True,
                },
                metadata=request.metadata,
            )
        params = request.params if isinstance(request.params, dict) else {}
        chrome_path = params.get("chrome_path")
        if not isinstance(chrome_path, str):
            return build_error_response(request, "chrome_path must be string", code="BAD_REQUEST")
        chrome_path = chrome_path.strip()

        raw_browser_type = params.get("browser_type", "auto")
        if not isinstance(raw_browser_type, str):
            return build_error_response(request, "browser_type must be string", code="BAD_REQUEST")
        normalized_type = raw_browser_type.strip().lower()
        if normalized_type in {"chrome", "google-chrome", "google_chrome"}:
            browser_type = "chrome"
        elif normalized_type in {"msedge", "edge", "microsoft-edge", "microsoft_edge"}:
            browser_type = "msedge"
        elif normalized_type in {"", "auto"}:
            browser_type = "auto"
        else:
            return build_error_response(
                request, "browser_type must be one of: auto, chrome, msedge", code="BAD_REQUEST"
            )

        headless = params.get("headless", True)
        if not isinstance(headless, bool):
            headless = True
        update_browser_in_config({"chrome_path": chrome_path, "browser_type": browser_type, "headless": headless})
        metadata = dict(request.metadata or {})
        metadata["config_changed"] = True
        metadata["browser_runtime_restart"] = True
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"chrome_path": chrome_path, "browser_type": browser_type, "headless": headless},
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
            payload = {"ok": True}
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
        from jiuwenswarm.gateway.channel_manager.tui.tui_connect import (
            CliHandlersBindParams,
            register_cli_handlers,
        )

        class _ReloadNoopClient:
            server_ready = True

            async def send_request(self, _envelope: Any) -> Any:
                # AgentWebSocketServer reloads the config after the adapter
                # returns, based on the metadata set below.
                return type("Response", (), {"ok": True, "payload": {}})()

        channel = _ConfigRpcChannel(request.channel_id or "tui")
        register_cli_handlers(
            CliHandlersBindParams(
                channel=channel,
                agent_client=_ReloadNoopClient(),
                force_local_config=True,
            )
        )
        handler = channel.methods.get(ReqMethod.COMMAND_MODEL.value)
        if handler is None:
            return build_error_response(request, "command.model handler unavailable")
        await handler(object(), request.request_id, request.params or {}, request.session_id)
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
