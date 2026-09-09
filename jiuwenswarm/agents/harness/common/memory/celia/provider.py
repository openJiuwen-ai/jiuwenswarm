"""Jiuwen MemoryProvider implementation backed by Celia MCP tools."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from openjiuwen.core.memory.external.provider import MemoryProvider

from .client_manager import CeliaClientLease, get_celia_client_manager
from .config import CeliaConfig
from .errors import CeliaError
from .fixed_context import get_fixed_context_cache
from .formatter import result_payload
from .prompt import load_celia_agent_prompt
from .runtime_context import CeliaRuntimeContext, resolve_runtime_context
from .sanitizer import clean_turn_events, sanitize_memory_text
from .tools import (
    ADVANCED_TOOLS,
    CORE_TOOLS,
    INTERNAL_TOOLS,
    MAX_CONTENT_BYTES,
    disabled_payload,
    tool_schemas,
    validate_arguments,
)
from .workspace_sync import sync_workspace_files

logger = logging.getLogger(__name__)


def _error_payload(
    tool_name: str,
    exc: Exception,
    *,
    context: CeliaRuntimeContext | None = None,
    db_path: str = "",
) -> str:
    message = _redact_diagnostic(str(exc))
    logger.warning(
        "[CeliaMemoryProvider] tool '%s' failed: method=tools/call exception=%s "
        "message=%s sessionId=%s db=%s",
        tool_name,
        type(exc).__name__,
        message,
        context.tool_session_id if context is not None else "",
        db_path,
    )
    return result_payload(None, ok=False, error="Celia memory operation failed", tool=tool_name)


def _redact_diagnostic(value: str) -> str:
    """Keep exception diagnostics useful without exposing credential values."""
    return re.sub(
        r"(?i)(api[_-]?key|authorization|token|secret|password)(\s*[:=]\s*)\S+",
        r"\1\2<redacted>",
        value[:2000],
    )


class CeliaMemoryProvider(MemoryProvider):
    def __init__(
        self,
        config: CeliaConfig,
        *,
        user_id: str = "__default__",
        scope_id: str = "user",
        session_id: str = "__default__",
        request_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = config
        self._default_user_id = user_id or config.user_id
        self._default_scope_id = scope_id or config.scope_id
        self._default_session_id = session_id or "__default__"
        self._request_metadata = dict(request_metadata or {})
        self._lease: CeliaClientLease | None = None
        self._initialized = False
        self._supported_mcp_tools: set[str] | None = None
        self._fixed_cache = get_fixed_context_cache()

    @property
    def name(self) -> str:
        return "celia"

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    def is_available(self) -> bool:
        return self.config.is_available()

    @property
    def client(self):
        return self._lease.client if self._lease else None

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        advanced = ADVANCED_TOOLS & set(self.config.advanced_tools) & (self._supported_mcp_tools or set())
        schemas = tool_schemas(advanced)
        if self._supported_mcp_tools is not None:
            schemas = [item for item in schemas if item["name"] in self._supported_mcp_tools]
        return schemas

    async def initialize(self, **kwargs: Any) -> None:
        if self._initialized:
            return
        stage = "preflight"
        lease: CeliaClientLease | None = None
        try:
            if self.config.preflight_enabled:
                issues = self.config.preflight_issues()
                if issues:
                    raise CeliaError("; ".join(issues))
            self._log_model_endpoint_diagnostics()
            stage = "acquire"
            lease = await get_celia_client_manager().acquire(self.config)
            stage = "tools/list"
            supported_tools = await lease.client.list_tools()
            missing = (CORE_TOOLS | INTERNAL_TOOLS) - supported_tools
            if missing:
                raise CeliaError("Celia MCP contract is missing tools: " + ", ".join(sorted(missing)))
            # The new MCP protocol has no memory_open. User and request scope
            # accompany each operation instead of opening a synthetic session.
        except Exception as exc:
            if lease is not None:
                await get_celia_client_manager().release(lease)
            logger.warning(
                "[CeliaMemoryProvider] initialization failed: stage=%s exception=%s message=%s db=%s log=%s",
                stage,
                type(exc).__name__,
                _redact_diagnostic(str(exc)),
                self.config.normalized_db_path,
                self.config.log_path,
            )
            raise
        self._lease = lease
        self._supported_mcp_tools = supported_tools
        self._initialized = True
        logger.info(
            "[CeliaMemoryProvider] initialized: tools=%d db=%s",
            len(supported_tools),
            self.config.normalized_db_path,
        )

    def _log_model_endpoint_diagnostics(self) -> None:
        missing = [
            f"OPENAI_{prefix}_{key}"
            for prefix, endpoint in (("CHAT", self.config.chat), ("EMBED", self.config.embed))
            for key, value in (
                ("BASE_URL", endpoint.base_url),
                ("API_KEY", endpoint.api_key),
                ("MODEL", endpoint.model),
            )
            if not value
        ]
        if missing:
            logger.warning(
                "[CeliaMemoryProvider] model endpoints incomplete: missing=%s; extraction or vector retrieval may fail",
                ", ".join(missing),
            )

    def _context(self, explicit: Mapping[str, Any] | None = None) -> CeliaRuntimeContext:
        return resolve_runtime_context(
            default_tenant_id=self.config.tenant_id,
            default_user_id=self._default_user_id,
            default_scope_id=self._default_scope_id,
            default_session_id=self._default_session_id,
            default_request_scope=self.config.request_scope,
            default_metadata=self._request_metadata,
            explicit={**dict(explicit or {}), "runtime_state_path": self.config.runtime_state_path},
        )

    @staticmethod
    def _scope_code(context: CeliaRuntimeContext) -> int:
        value = str(context.scope_id).strip().lower()
        if value in {"", "__default__", "user", "1"}:
            return 1
        if value in {"global", "0"}:
            return 0
        if value in {"session", "3"}:
            return 3
        raise CeliaError("Unsupported Celia authorization scope; expected global, user or session")

    def _wire_arguments(
        self, name: str, args: Mapping[str, Any], context: CeliaRuntimeContext
    ) -> dict[str, Any]:
        if name == "memory_update_config":
            return dict(args)
        scope = self._scope_code(context)
        result = {
            **dict(args),
            "userId": context.user_id,
            "traceId": context.trace_id,
            "requestScope": dict(context.request_scope),
        }
        # User-scoped reads must include prior conversations. Supply a session
        # filter only for session authorization; writes retain their provenance.
        if scope == 3 or name in {"memory_add", "memory_store", "memory_restore"}:
            result["sessionId"] = context.conversation_id
        if name == "memory_store":
            result["scope"] = scope
        elif name == "memory_record_search":
            result["scopeFilter"] = scope
        return result

    def _cache_key(self, context: CeliaRuntimeContext) -> str:
        return json.dumps(
            [
                self.config.normalized_db_path,
                context.tenant_id,
                context.user_id,
                context.scope_id,
                context.request_scope,
                context.conversation_id if self._scope_code(context) == 3 else "",
            ],
            sort_keys=True,
            ensure_ascii=False,
        )

    async def _call(
        self,
        tool_name: str,
        args: dict[str, Any],
        context: CeliaRuntimeContext,
        *,
        timeout_ms: int | None = None,
    ) -> object:
        if not self._initialized or not self._lease:
            raise CeliaError("Celia provider is not initialized")
        if self._supported_mcp_tools is not None and tool_name not in self._supported_mcp_tools:
            raise CeliaError(f"Celia backend does not support {tool_name}")
        wire = self._wire_arguments(tool_name, args, context)
        validate_arguments(tool_name, wire)
        try:
            # traceId is already injected in the wire arguments. The config
            # update schema permits only updates, so do not add trace fields.
            return await self._lease.client.call_tool(tool_name, wire, timeout_ms=timeout_ms)
        except Exception as exc:
            logger.warning(
                "[CeliaMemoryProvider] MCP call failed: tool=%s exception=%s message=%s conversationId=%s db=%s",
                tool_name,
                type(exc).__name__,
                _redact_diagnostic(str(exc)),
                context.conversation_id,
                self.config.normalized_db_path,
            )
            raise

    async def prefetch(self, query: str, **kwargs: Any) -> str:
        if not self._initialized:
            return ""
        context = self._context(kwargs)
        if not context.memory_state:
            return ""
        try:
            return await self._fixed_cache.get(
                self._cache_key(context), lambda: self._load_fixed_context(context)
            )
        except Exception as exc:
            logger.warning("[CeliaMemoryProvider] global prefetch failed: %s", _redact_diagnostic(str(exc)))
            return ""

    async def _load_fixed_context(self, context: CeliaRuntimeContext) -> str:
        result = await self._call("memory_global_load", {}, context)
        # No response schema was supplied with the new input contract. Preserve
        # the complete envelope so navigation/scene IDs are not lost by guessing
        # a field name. Scene discovery remains available through scene_search.
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
        if not text or result in (None, {}, []):
            return ""
        from pathlib import Path

        if self.config.workspace_dir:
            await asyncio.to_thread(
                sync_workspace_files,
                Path(self.config.workspace_dir),
                {"global_summary": text},
                [],
                Path.home() / ".openclaw" / ".memory.log",
                sync_l1=False,
            )
        return "## CELIA_MEMORY_OVERVIEW\n" + text

    async def handle_tool_call(self, tool_name: str, args: dict[str, Any]) -> str:
        # Only host lifecycle hooks may pass explicit identity to _context.
        # Model arguments are validated separately and never become identity.
        context = self._context()
        try:
            if tool_name not in {item["name"] for item in self.get_tool_schemas()}:
                raise ValueError(f"Celia tool is not enabled: {tool_name}")
            validate_arguments(tool_name, args, model_facing=True)
            needs_extraction = tool_name in {
                "memory_store",
                "memory_global_load",
                "memory_scene_load",
                "memory_scene_search",
            } or (tool_name == "memory_record_search" and args["searchType"] == "atomic_fact")
            if needs_extraction and not context.memory_state:
                return disabled_payload(tool_name)
            forwarded = dict(args)
            if tool_name == "memory_store":
                content = sanitize_memory_text(forwarded["content"])
                if not content:
                    return result_payload(None, ok=False, status="rejected")
                forwarded["content"] = content
            result = await self._call(tool_name, forwarded, context)
            if tool_name == "memory_store" or (tool_name == "memory_restore" and not args.get("dryRun", 0)):
                self._fixed_cache.mark_dirty(self._cache_key(context))
            elif tool_name == "memory_update_config":
                self._fixed_cache.clear()
            return result_payload(result)
        except Exception as exc:
            return _error_payload(tool_name, exc, context=context, db_path=self.config.normalized_db_path)

    async def sync_turn(self, user_msg: str, assistant_msg: str, **kwargs: Any) -> None:
        if not self._initialized:
            return
        context = self._context(kwargs)
        events = kwargs.get("events")
        if not isinstance(events, list):
            events = [{"role": "user", "text": user_msg}, {"role": "assistant", "text": assistant_msg}]
        messages = clean_turn_events(events, text_limit=None)
        if not any(item.get("role") in {"user", "assistant"} for item in messages):
            messages = clean_turn_events(
                [
                    {"role": "user", "text": user_msg},
                    {"role": "assistant", "text": assistant_msg},
                ],
                text_limit=None,
            )
        for message in messages:
            # Preserve sanitized tool/trace details in content; they are not
            # extra wire fields and do not require an unsupported role code.
            content = (
                message.get("text")
                if set(message) <= {"role", "text"}
                else json.dumps(message, ensure_ascii=False)
            )
            if not content:
                continue
            # Each add has one role. A mixed user/assistant JSON array must not
            # be mislabelled as a single user message by the new protocol.
            remaining = content.encode("utf-8")
            while remaining:
                chunk = remaining[:MAX_CONTENT_BYTES].decode("utf-8", errors="ignore")
                remaining = remaining[len(chunk.encode("utf-8")) :]
                await self._call(
                    "memory_add",
                    {
                        "content": chunk,
                        "role": 0 if message["role"] == "user" else 1,
                        "skipExtraction": 0 if context.memory_state else 1,
                    },
                    context,
                )
        if messages:
            self._fixed_cache.mark_dirty(self._cache_key(context))

    async def report_round_usage(self, **kwargs: Any) -> None:
        """Retain the rail hook without calling the removed usage-report tool."""

    def system_prompt_block(self) -> str:
        base = load_celia_agent_prompt()
        runtime_state = (
            f"The real compatibility state is at {self.config.runtime_state_path}; "
            "MEMORYSTATE=false disables L1/L2 extraction but keeps L3."
        )
        return "\n\n".join(part for part in (base, runtime_state) if part)

    async def on_session_end(self, messages=None) -> None:
        self._fixed_cache.clear(self._cache_key(self._context()))

    async def shutdown(self) -> None:
        lease, self._lease = self._lease, None
        self._initialized = False
        self._supported_mcp_tools = None
        if lease is not None:
            await get_celia_client_manager().release(lease)
