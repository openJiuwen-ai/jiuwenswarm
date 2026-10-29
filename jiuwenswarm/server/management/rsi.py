# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rsi management; resources remain owned by its injected host."""

from __future__ import annotations

import logging
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.agent import AgentResponse
from typing import Any
from pathlib import Path
import asyncio
import inspect
import json
import os

logger = logging.getLogger(__name__)


class RsiManagement:
    def __init__(self, host):
        self.host = host

    async def _ensure_persistent_checkpointer_response(
        self, request: AgentRequest
    ) -> AgentResponse | None:
        """Return an error response when persistent checkpoint storage is unavailable."""
        host = self.host
        try:
            from jiuwenswarm.runtime.checkpointer import ensure_persistent_checkpointer
    
            await ensure_persistent_checkpointer()
            await host._try_start_heartbeat_runtime()
            return None
        except Exception as exc:
            logger.exception(
                "[AgentWebSocketServer] persistent checkpointer unavailable: request_id=%s error=%s",
                request.request_id,
                exc,
            )
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={
                    "error": "persistent checkpointer is unavailable",
                    "code": "CHECKPOINT_UNAVAILABLE",
                },
                metadata=request.metadata,
            )

    async def _handle_rsi_request(
        self, ws: Any, request: AgentRequest, send_lock: asyncio.Lock
    ) -> None:
        """Handle any rsi.* unary method (including Harness installation).

        Builds the RSI service context lazily (one per server process) and
        wires:
        - harness_refs 快照提供方 = RSI active version, then controlled generic fallback;
        - send_push 包装 = ``self.send_push``（E2A server_push，零改动）。
        """
        host = self.host
        try:
            handlers = host._get_rsi_handlers()
            handle_async = getattr(handlers, "handle_async", None)
            if callable(handle_async):
                result = handle_async(request)
            else:
                result = handlers.handle(request)
            if inspect.isawaitable(result):
                result = await result
            if result.get("ok"):
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=True,
                    payload=result.get("payload"),
                )
            else:
                resp = AgentResponse(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    ok=False,
                    payload={
                        "error": str(result.get("error") or "rsi request failed"),
                        "code": str(result.get("code") or "INTERNAL_ERROR"),
                    },
                )
        except Exception as exc:
            logger.exception("[AgentServer] rsi request failed: %s", exc)
            resp = AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=False,
                payload={"error": str(exc), "code": "INTERNAL_ERROR"},
            )
        wire = host.services.encode_response(resp, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)

    def _get_rsi_handlers(self):
        """Lazily construct the RSI service context + AgentServer handlers once."""
        host = self.host
        if host._rsi_handlers is not None:
            return host._rsi_handlers
        from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
        from jiuwenswarm.server.rsi import RsiAgentServerHandlers

        provider_mode = os.environ.get("RSI_PROVIDER_MODE", "").strip().lower()
        if not provider_mode:
            provider_mode = (
                "mock"
                if os.environ.get("RSI_USE_MOCK_PROVIDER", "").strip().lower() == "true"
                else "real"
            )
        context = build_rsi_service_context(
            None,
            enable_harness_materialization=True,
            allow_missing_harness=provider_mode == "mock",
        )
        if provider_mode == "mock":
            from jiuwenswarm.agents.harness.common.rsi.provider_factory import (
                build_rsi_adapters,
            )

            context.register_adapters(
                build_rsi_adapters(
                    context.tasks_root, mode="mock", model_resolver=host._resolve_model
                )
            )
        else:
            harness_provider = getattr(host, "_rsi_harness_provider", None)
            if harness_provider is None:
                from jiuwenswarm.agents.harness.common.rsi.harness_provider import (
                    HarnessProvider,
                )

                harness_provider = HarnessProvider(
                    context.tasks_root, model_resolver=context.model_resolver
                )
                host._rsi_harness_provider = harness_provider
            from jiuwenswarm.agents.harness.common.rsi.provider_factory import (
                build_rsi_adapters,
            )

            context.register_adapters(
                build_rsi_adapters(
                    context.tasks_root, mode="real", model_resolver=host._resolve_model
                )
            )
            context.register_harness_provider(harness_provider)
        context.bind_harness_installer(host._agent_manager)
        handlers = RsiAgentServerHandlers(
            context,
            send_push=host.send_push,
            harness_refs_provider=None
            if provider_mode == "mock"
            else host._rsi_harness_refs_provider,
            default_channel_id="web",
        )
        host._rsi_handlers = handlers
        return handlers

    @staticmethod
    def _rsi_harness_refs_provider(params: dict[str, Any] | None = None) -> str | None:
        """Resolve an explicit installed Plugin, or use the active Harness.
    
        ``package_id`` uses the same Plugin registry as chat. Omitting it keeps
        the existing baseline fallback; ``harness_id`` remains a legacy registry
        selector. Arbitrary browser-supplied paths are not resolved here.
        """
        from jiuwenswarm.agents.harness.common.rsi.context import get_rsi_workspace_root
        from jiuwenswarm.agents.harness.common.rsi.harness_activation import (
            RsiHarnessActivationStore,
            resolve_native_harness_baseline,
        )
        from jiuwenswarm.common.utils import (
            get_user_workspace_dir as current_workspace_dir,
        )
        requested_id = str(
            (params or {}).get("package_id") or ""
        ).strip()
        if requested_id:
            from jiuwenswarm.agents.harness.common.rsi.errors import RsiInvalidHarness
            from jiuwenswarm.server.runtime import (
                extension_package_manager as equipment,
            )

            # Resolve the existing package selector through the same registry
            # as chat.send. Never silently replace an explicit selection by H0.
            try:
                if not equipment.is_plugin_allowed(requested_id):
                    raise ValueError(f"Plugin is not installed: {requested_id}")
                package = equipment.resolve_plugin_dir(requested_id)
                manifest = json.loads(
                    (package / "manifest.json").read_text(encoding="utf-8")
                )
                if manifest.get("mcps"):
                    raise ValueError(
                        "RSI isolated evaluation does not yet support Plugin MCP dependencies"
                    )
                return str(package.resolve())
            except (ValueError, OSError) as exc:
                raise RsiInvalidHarness(str(exc)) from exc
        try:
            active = RsiHarnessActivationStore(
                get_rsi_workspace_root() / "tasks"
            ).resolve_active_runtime_path()
            if active:
                return active
        except Exception as exc:
            logger.warning(
                "[RSI] active Harness 定位失败，回退 generic registry: %s", exc
            )
        configured_harness_root = os.environ.get("RSI_HARNESS_ROOT", "").strip()
        harness_root = (
            Path(configured_harness_root).expanduser().resolve()
            if configured_harness_root
            else (Path(current_workspace_dir()) / "rsi" / "harness").resolve()
        )
        initial_refs = harness_root / "initial_harness_refs.yaml"
        if initial_refs.is_file():
            return str(initial_refs)
        from jiuwenswarm.agents.harness.common.auto_harness.service import (
            _HARNESS_PACKAGES_FILE,
        )
    
        try:
            data = {}
            if _HARNESS_PACKAGES_FILE.is_file():
                with _HARNESS_PACKAGES_FILE.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
            active_ids = data.get("active_package_ids") or []
            packages = data.get("packages") or []
            by_id = {str(p.get("id")): p for p in packages if isinstance(p, dict)}
            legacy_id = str((params or {}).get("harness_id") or "").strip()
            for package_id in [legacy_id] if legacy_id else active_ids:
                package = by_id.get(str(package_id))
                if not package:
                    continue
                runtime_path = str(package.get("runtime_path") or "")
                if runtime_path and Path(runtime_path).expanduser().is_dir():
                    return str(Path(runtime_path).expanduser().resolve())
                config_path = str(package.get("config_path") or "")
                if config_path and Path(config_path).expanduser().is_file():
                    return str(Path(config_path).expanduser().resolve())
        except Exception as exc:
            logger.warning("[RSI] harness refs 定位失败: %s", exc)
        baseline = resolve_native_harness_baseline()
        if baseline is not None:
            logger.info("[RSI] No active Harness found; using native Agent baseline")
            return str(baseline)
        return None
