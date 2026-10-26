from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, Optional, TypeVar

from openjiuwen.harness.tools.cron import CronToolBackend, CronToolContext, create_cron_tools

from jiuwenswarm.gateway.cron import CronTargetChannel
from jiuwenswarm.gateway.cron.dingtalk_routing import (
    build_dingtalk_cron_session_id_from_context,
    dingtalk_chat_type_from_metadata,
)
from jiuwenswarm.gateway.cron.models import (
    CRON_JOB_DEFAULT_MODE,
    coerce_cron_job_mode,
    is_valid_target_channel_id,
    normalize_target_channel_id,
)
from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import CronToolRoute, CronTools
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.common.schema.message import Message, ReqMethod
from jiuwenswarm.common.utils import logger
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

_T = TypeVar("_T")


def _bound_request_context() -> Any | None:
    """Use the live request ContextVar binding when DeepAgent omits ``context``.

    Only the current bound invocation is trusted. Unbound leftover ContextVars
    are ignored; session adapters inject the same-request proxy instead.
    """
    try:
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            _CRON_TOOL_BOUND,
            _CRON_TOOL_MODE,
            get_runtime_tool_channel_id,
            get_runtime_tool_metadata,
            get_runtime_tool_session_id,
        )
    except Exception:
        return None
    if not _CRON_TOOL_BOUND.get():
        return None
    metadata = get_runtime_tool_metadata()
    return SimpleNamespace(
        channel_id=get_runtime_tool_channel_id(),
        session_id=get_runtime_tool_session_id(),
        metadata=dict(metadata) if isinstance(metadata, dict) else metadata,
        mode=_CRON_TOOL_MODE.get(),
    )


def _normalize_tenant_scope(
    service_id: str | None,
    agent_id: str | None,
) -> tuple[str, str]:
    return (
        TenantAgentPool.normalize_tenant_id(service_id),
        TenantAgentPool.normalize_tenant_id(agent_id),
    )


def _tenant_scope_from_context(context: Any | None) -> tuple[str, str]:
    if context is None:
        return "default", "default"
    metadata = getattr(context, "metadata", None)
    if isinstance(metadata, dict):
        sid = metadata.get("service_id")
        aid = metadata.get("agent_id")
        if sid is not None or aid is not None:
            return _normalize_tenant_scope(
                str(sid) if sid is not None else None,
                str(aid) if aid is not None else None,
            )
    return "default", "default"


class _CronToolsCronBackend(CronToolBackend):
    """Adapt AgentServer CronTools to the DeepAgents CronToolBackend interface."""

    def __init__(self, cron_tools: CronTools, message_handler: MessageHandler | None = None) -> None:
        self._cron_tools = cron_tools
        self._message_handler = message_handler

    @staticmethod
    def _route_from_context(context: CronToolContext | None) -> CronToolRoute:
        if context is None:
            return CronToolRoute()
        metadata = context.metadata if isinstance(context.metadata, dict) else {}
        request_id = str(metadata.get("request_id") or "").strip()
        channel_id = str(context.channel_id or "").strip() or CronTargetChannel.WEB.value
        session_id = (
            str(context.session_id).strip()
            if isinstance(context.session_id, str) and context.session_id.strip()
            else None
        )
        chat_type = str(metadata.get("chat_type") or "").strip() or None
        # 钉钉入站用 conversation_type(1/2)，需映射到 cron 的 group/p2p，供推送路由使用。
        # create_job 以 route.session_id 落盘，这里必须写入 delivery binding，
        # 不能把 Gateway 内部 dingtalk_… 会话 ID 当成钉钉 staffId。
        if channel_id == "dingtalk" or channel_id.startswith("dingtalk:"):
            if not chat_type:
                chat_type = dingtalk_chat_type_from_metadata(metadata)
            bound_sid = build_dingtalk_cron_session_id_from_context(
                session_id=session_id,
                metadata=metadata,
            )
            if bound_sid:
                session_id = bound_sid
        project_dir = str(metadata.get("project_dir") or "").strip()
        project_id = str(metadata.get("project_id") or "").strip()
        work_mode = str(metadata.get("work_mode") or "").strip()
        app_id = str(metadata.get("app_id") or "").strip()
        from jiuwenswarm.gateway.cron.enterprise_gate import extract_routing_triple

        group_id, bot_id, user_id = extract_routing_triple(metadata)
        return CronToolRoute(
            request_id=request_id,
            channel_id=channel_id,
            session_id=session_id,
            chat_type=chat_type,
            project_dir=project_dir,
            project_id=project_id,
            work_mode=work_mode,
            app_id=app_id,
            group_id=group_id,
            bot_id=bot_id,
            user_id=user_id,
        )

    def _assert_enterprise_route(self, route: CronToolRoute) -> None:
        ready_fn = getattr(self._cron_tools, "_enterprise_ready", None)
        if not callable(ready_fn) or not ready_fn():
            return
        from jiuwenswarm.gateway.cron.enterprise_gate import routing_triple_complete

        if not str(route.session_id or "").strip() or not routing_triple_complete(
            route.group_id, route.bot_id, route.user_id
        ):
            raise ValueError("enterprise cron requires an authenticated session")

    async def _with_route(
        self,
        context: CronToolContext | None,
        op: Callable[[], Awaitable[_T]],
    ) -> _T:
        effective = context if context is not None else _bound_request_context()
        route = self._route_from_context(effective)
        self._assert_enterprise_route(route)
        token = self._cron_tools.push_cron_route(route)
        try:
            return await op()
        finally:
            self._cron_tools.reset_cron_route(token)

    async def list_jobs(
        self,
        *,
        include_disabled: bool = True,
        context: CronToolContext | None = None,
    ) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            jobs = await self._cron_tools.list_jobs()
            rows = [self._to_backend_job(job) for job in jobs]
            if include_disabled:
                return rows
            return [job for job in rows if job.get("enabled", True)]

        return await self._with_route(context, _run)

    async def get_job(
        self,
        job_id: str,
        *,
        context: CronToolContext | None = None,
    ) -> dict[str, Any] | None:
        async def _run() -> dict[str, Any] | None:
            job = await self._cron_tools.get_job(job_id)
            if job is None:
                return None
            return self._to_backend_job(job)

        return await self._with_route(context, _run)

    async def create_job(
        self,
        params: dict[str, Any],
        *,
        context: CronToolContext | None = None,
    ) -> dict[str, Any]:
        request_id = None
        if context and isinstance(context.metadata, dict):
            request_id = context.metadata.get("request_id")
        logger.info(
            (
                "[CronRuntimeBridge] create_job in: context.channel_id=%s "
                "context.session_id=%s metadata.request_id=%s raw_keys=%s"
            ),
            getattr(context, "channel_id", None),
            getattr(context, "session_id", None),
            request_id,
            sorted(list((params or {}).keys())),
        )
        payload = _extract_legacy_params(dict(params or {}), context=context, require_schedule=True)
        logger.info(
            "[CronRuntimeBridge] create_job mapped payload.targets=%s payload.id=%s payload.name=%s",
            payload.get("targets"),
            payload.get("id"),
            payload.get("name"),
        )

        async def _run() -> dict[str, Any]:
            job = await self._cron_tools.create_job(payload)
            return self._to_backend_job(job)

        return await self._with_route(context, _run)

    async def update_job(
        self,
        job_id: str,
        patch: dict[str, Any],
        *,
        context: CronToolContext | None = None,
    ) -> dict[str, Any]:
        payload = _extract_legacy_params(dict(patch or {}), context=context, require_schedule=False)

        async def _run() -> dict[str, Any]:
            job = await self._cron_tools.update_job(job_id, payload)
            return self._to_backend_job(job)

        return await self._with_route(context, _run)

    async def delete_job(
        self,
        job_id: str,
        *,
        context: CronToolContext | None = None,
    ) -> bool:
        async def _run() -> bool:
            return bool(await self._cron_tools.delete_job(job_id))

        return await self._with_route(context, _run)

    async def toggle_job(
        self,
        job_id: str,
        enabled: bool,
        *,
        context: CronToolContext | None = None,
    ) -> dict[str, Any]:
        async def _run() -> dict[str, Any]:
            job = await self._cron_tools.toggle_job(job_id, enabled)
            return self._to_backend_job(job)

        return await self._with_route(context, _run)

    async def preview_job(
        self,
        job_id: str,
        count: int = 5,
        *,
        context: CronToolContext | None = None,
    ) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            rows = await self._cron_tools.preview_job(job_id, count)
            return list(rows or [])

        return await self._with_route(context, _run)

    async def run_now(
        self,
        job_id: str,
        *,
        context: CronToolContext | None = None,
    ) -> str:
        async def _run() -> str:
            run_result = await self._cron_tools.run_now(job_id)
            if isinstance(run_result, dict):
                return str(run_result.get("run_id") or "")
            return str(run_result or "")

        return await self._with_route(context, _run)

    async def status(self, *, context: CronToolContext | None = None) -> dict[str, Any]:
        jobs = await self.list_jobs(include_disabled=True, context=context)
        return {
            "running": False,
            "job_count": len(jobs),
            "run_count": 0,
        }

    async def get_runs(self, job_id: str, limit: int = 20) -> list[dict[str, Any]]:
        _ = (job_id, limit)
        return []

    async def wake(
        self,
        text: str,
        *,
        context: CronToolContext | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        if not text.strip():
            raise ValueError("text is required")
        if context is None or not (context.channel_id or "").strip():
            raise ValueError("wake requires an active session context")
        if self._message_handler is None:
            raise RuntimeError("cron wake is unavailable before message handler startup")

        msg = Message(
            id=f"cron-wake-{int(time.time() * 1000)}",
            type="req",
            channel_id=context.channel_id,
            session_id=context.session_id,
            params={
                "query": text,
                "content": text,
                "mode": (mode or context.mode or CRON_JOB_DEFAULT_MODE),
            },
            timestamp=time.time(),
            ok=True,
            req_method=ReqMethod.CHAT_SEND,
            metadata=deepcopy(context.metadata) if isinstance(context.metadata, dict) else None,
        )
        await self._message_handler.publish_user_messages(msg)
        return {"queued": True}

    async def ensure_scheduler_started(self) -> None:
        """确保scheduler已启动，如果未启动则异步启动"""
        await self._cron_tools.ensure_scheduler()

    @staticmethod
    def _to_backend_job(job: dict[str, Any]) -> dict[str, Any]:
        row = dict(job)
        row.setdefault(
            "schedule",
            {
                "kind": "cron",
                "expr": str(row.get("cron_expr") or "").strip(),
                "tz": str(row.get("timezone") or "Asia/Shanghai").strip() or "Asia/Shanghai",
            },
        )
        row.setdefault(
            "payload",
            {
                "kind": "agentTurn",
                "message": str(row.get("description") or "").strip(),
            },
        )
        row.setdefault(
            "delivery",
            {
                "mode": "announce",
                "channel": str(
                    row.get("targets") or CronTargetChannel.WEB.value).strip() or CronTargetChannel.WEB.value,
            },
        )
        row.setdefault("session_target", "isolated")
        row.setdefault("compat_mode", "legacy")
        return row


class _ContextInjectingCronBackend:
    """Fill omitted DeepAgent context from the session adapter proxy.

    openjiuwen ``create_cron_tools`` only forwards ``context=`` on create/update/wake.
    list/get/delete/toggle/preview/status/run call the backend without it, so
    ``_with_route`` would otherwise see an empty CronToolRoute. The adapter's
    ``_RuntimeCronToolContext`` already remembers per-request identity for
    create; wrap the shared tenant backend with that same object at tool-build
    time so invoke_tool list/get reuse it. Personal edition is unchanged: extra
    identity on the route is unused when enterprise cron is off.
    """

    def __init__(self, inner: CronToolBackend, context: Any) -> None:
        self._inner = inner
        self._context = context

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _ctx(self, context: CronToolContext | None) -> Any:
        return context if context is not None else self._context

    async def list_jobs(
        self,
        *,
        include_disabled: bool = True,
        context: CronToolContext | None = None,
    ) -> list[dict[str, Any]]:
        return await self._inner.list_jobs(
            include_disabled=include_disabled,
            context=self._ctx(context),
        )

    async def get_job(
        self,
        job_id: str,
        *,
        context: CronToolContext | None = None,
    ) -> dict[str, Any] | None:
        return await self._inner.get_job(job_id, context=self._ctx(context))

    async def create_job(
        self,
        params: dict[str, Any],
        *,
        context: CronToolContext | None = None,
    ) -> dict[str, Any]:
        return await self._inner.create_job(params, context=self._ctx(context))

    async def update_job(
        self,
        job_id: str,
        patch: dict[str, Any],
        *,
        context: CronToolContext | None = None,
    ) -> dict[str, Any]:
        return await self._inner.update_job(job_id, patch, context=self._ctx(context))

    async def delete_job(
        self,
        job_id: str,
        *,
        context: CronToolContext | None = None,
    ) -> bool:
        return await self._inner.delete_job(job_id, context=self._ctx(context))

    async def toggle_job(
        self,
        job_id: str,
        enabled: bool,
        *,
        context: CronToolContext | None = None,
    ) -> dict[str, Any]:
        return await self._inner.toggle_job(
            job_id, enabled, context=self._ctx(context)
        )

    async def preview_job(
        self,
        job_id: str,
        count: int = 5,
        *,
        context: CronToolContext | None = None,
    ) -> list[dict[str, Any]]:
        return await self._inner.preview_job(
            job_id, count, context=self._ctx(context)
        )

    async def run_now(
        self,
        job_id: str,
        *,
        context: CronToolContext | None = None,
    ) -> str:
        return await self._inner.run_now(job_id, context=self._ctx(context))

    async def status(self, *, context: CronToolContext | None = None) -> dict[str, Any]:
        return await self._inner.status(context=self._ctx(context))

    async def wake(
        self,
        text: str,
        *,
        context: CronToolContext | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        return await self._inner.wake(text, context=self._ctx(context), mode=mode)


def _extract_legacy_params(
    payload: dict[str, Any],
    *,
    context: CronToolContext | None,
    require_schedule: bool,
) -> dict[str, Any]:
    data = dict(payload or {})
    context_channel = str((context.channel_id if context else "") or "").strip()
    context_target = ""
    if context_channel:
        if context_channel.startswith("feishu_enterprise:"):
            context_target = normalize_target_channel_id(
                context_channel,
                default=CronTargetChannel.WEB.value,
            )
        elif is_valid_target_channel_id(context_channel):
            context_target = context_channel
    if "schedule" in data or "payload" in data or "delivery" in data:
        schedule = data.get("schedule") if isinstance(data.get("schedule"), dict) else {}
        kind = str(schedule.get("kind") or "cron").strip().lower()

        cron_expr = str(
            schedule.get("expr")
            or schedule.get("cron")
            or data.get("cron_expr")
            or ""
        ).strip()
        timezone = str(
            schedule.get("tz")
            or schedule.get("timezone")
            or data.get("timezone")
            or "Asia/Shanghai"
        ).strip() or "Asia/Shanghai"

        if kind == "at":
            at_raw = str(schedule.get("at") or "").strip()
            if at_raw:
                try:
                    from jiuwenswarm.gateway.cron.cron_expr import iso_to_seven_field_cron
                    cron_expr = iso_to_seven_field_cron(at_raw, timezone=timezone)
                    logger.info(
                        "[CronRuntimeBridge] _extract_legacy_params: converted kind=at '%s' to cron_expr='%s'",
                        at_raw, cron_expr,
                    )
                except Exception as conv_exc:
                    raise ValueError(
                        f"Cannot convert schedule.at='{at_raw}' to cron expression: {conv_exc}"
                    ) from conv_exc
            else:
                raise ValueError("schedule.kind='at' requires schedule.at field with ISO datetime")
        elif kind and kind != "cron":
            raise ValueError(
                f"Unsupported schedule.kind='{kind}'. Only 'cron' and 'at' are supported by the gateway bridge"
            )

        payload_block = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        payload_kind = str(payload_block.get("kind") or "agentTurn").strip()
        if payload_kind == "systemEvent":
            logger.info(
                "[CronRuntimeBridge] _extract_legacy_params: converting payload.kind=systemEvent to agentTurn"
            )
            payload_kind = "agentTurn"
        elif payload_kind and payload_kind != "agentTurn":
            raise ValueError(
                f"Unsupported payload.kind='{payload_kind}'. Only 'agentTurn' and 'systemEvent' are supported"
            )
        description = str(
            payload_block.get("message")
            or payload_block.get("text")
            or data.get("description")
            or ""
        )

        delivery = data.get("delivery") if isinstance(data.get("delivery"), dict) else {}
        logger.info(
            "[CronRuntimeBridge] _extract_legacy_params: delivery.channel=%s data.targets=%s context.channel_id=%s",
            delivery.get("channel"),
            data.get("targets"),
            (context.channel_id if context else None),
        )
        targets = str(
            delivery.get("channel")
            or data.get("targets")
            or (context.channel_id if context else "")
            or CronTargetChannel.WEB.value
        ).strip() or CronTargetChannel.WEB.value
        # Per-request routing: when DeepAgent tool injects implicit delivery.channel=web,
        # use current request context channel instead of sticky tool-level default.
        has_context_target = bool(context_target)
        is_web_target = targets == CronTargetChannel.WEB.value
        has_explicit_targets = "targets" in data
        has_delivery_channel = "channel" in delivery
        should_use_context_target = (
            has_context_target
            and is_web_target
            and not has_explicit_targets
            and has_delivery_channel
        )
        if should_use_context_target:
            logger.info(
                "[CronRuntimeBridge] map implicit web target to request context: %s -> %s",
                targets,
                context_target,
            )
            targets = context_target
        logger.info(
            "[CronRuntimeBridge] _extract_legacy_params: resolved targets=%s",
            targets,
        )

        out: dict[str, Any] = {}
        if cron_expr or require_schedule:
            out["cron_expr"] = cron_expr
        if timezone or require_schedule:
            out["timezone"] = timezone
        if description:
            out["description"] = description
        if targets:
            out["targets"] = targets
        if "name" in data:
            out["name"] = str(data.get("name") or "").strip()
        if "id" in data:
            out["id"] = str(data.get("id") or "").strip()
        if "enabled" in data:
            out["enabled"] = bool(data.get("enabled"))
        if "wake_offset_seconds" in data:
            out["wake_offset_seconds"] = data.get("wake_offset_seconds")
        if "deleteAfterRun" in data:
            out["delete_after_run"] = bool(data.get("deleteAfterRun"))

        context_session_id = getattr(context, "session_id", None)
        context_metadata = getattr(context, "metadata", None) or {}
        if not isinstance(context_metadata, dict):
            context_metadata = {}

        # 钉钉：把发起会话编码进 session_id，避免推送时误用全局 last_*（Issue #2449）。
        target_channel = str(out.get("targets") or getattr(context, "channel_id", None) or "").strip()
        if target_channel == "dingtalk" or target_channel.startswith("dingtalk:"):
            bound_sid = build_dingtalk_cron_session_id_from_context(
                session_id=context_session_id if isinstance(context_session_id, str) else None,
                metadata=context_metadata,
            )
            if bound_sid:
                out["session_id"] = bound_sid
                logger.info(
                    "[CronRuntimeBridge] _extract_legacy_params: bound dingtalk session_id=%s",
                    out["session_id"],
                )
            elif isinstance(context_session_id, str) and context_session_id.strip():
                out["session_id"] = context_session_id.strip()
        elif isinstance(context_session_id, str) and context_session_id.strip():
            out["session_id"] = context_session_id.strip()
            logger.info(
                "[CronRuntimeBridge] _extract_legacy_params: added session_id=%s from context",
                out["session_id"],
            )

        # 飞书多应用：传递 app_id，用于调度器定位正确的 app 配置
        context_app_id = str(context_metadata.get("app_id") or "").strip()
        if context_app_id:
            out["app_id"] = context_app_id

        context_mode = getattr(context, "mode", None)
        mode_resolved = context_mode or data.get("mode") or CRON_JOB_DEFAULT_MODE
        out["mode"] = coerce_cron_job_mode(mode_resolved, default=CRON_JOB_DEFAULT_MODE)
        return out

    return data


class CronRuntimeBridge:
    """Resolve the host cron backend for DeepAgents while keeping gateway diffs minimal."""

    def __init__(self) -> None:
        self._backend_override: CronToolBackend | None = None
        self._resolved_backends: dict[tuple[str, str], CronToolBackend] = {}

    def set_backend(self, backend: CronToolBackend | None) -> None:
        self._backend_override = backend
        self._resolved_backends.clear()
        if backend is not None:
            self._resolved_backends[("default", "default")] = backend

    def get_backend(
        self,
        service_id: str | None = None,
        agent_id: str | None = None,
    ) -> CronToolBackend | None:
        if self._backend_override is not None:
            return self._backend_override

        sid, aid = _normalize_tenant_scope(service_id, agent_id)
        cache_key = (sid, aid)
        cached = self._resolved_backends.get(cache_key)
        if cached is not None:
            return cached

        message_handler = None
        try:
            message_handler = MessageHandler.get_instance()
        except RuntimeError:
            message_handler = None

        from jiuwenswarm.server.runtime.cron_local_runtime import AgentCronRegistry

        cron_tools = AgentCronRegistry.get_or_create(
            sid,
            aid,
            factory=lambda: CronTools(service_id=sid, agent_id=aid),
        )
        backend: CronToolBackend = _CronToolsCronBackend(
            cron_tools,
            message_handler=message_handler,
        )
        self._resolved_backends[cache_key] = backend
        logger.info(
            "[CronRuntimeBridge] CronTools backend initialized for service_id=%s agent_id=%s",
            sid,
            aid,
        )
        return backend

    async def remove_tenant(
        self,
        service_id: str | None = None,
        agent_id: str | None = None,
    ) -> bool:
        """Drop cached backend and stop the shared Agent-side scheduler for the tenant."""
        sid, aid = _normalize_tenant_scope(service_id, agent_id)
        self._resolved_backends.pop((sid, aid), None)
        from jiuwenswarm.server.runtime.cron_local_runtime import AgentCronRegistry

        return await AgentCronRegistry.remove(sid, aid)

    def ensure_scheduler_started(
        self,
        service_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """确保scheduler已启动，如果未启动则异步启动"""
        backend = self.get_backend(service_id=service_id, agent_id=agent_id)
        if backend is None:
            return

        if not isinstance(backend, _CronToolsCronBackend):
            return

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(backend.ensure_scheduler_started())
            else:
                loop.run_until_complete(backend.ensure_scheduler_started())
        except Exception as exc:
            logger.warning("[CronRuntimeBridge] Failed to start scheduler: %s", exc)

    def build_tools(
        self,
        *,
        context: Any,
        agent_id: Optional[str],
        language: str = "cn",
        service_id: str | None = None,
        tenant_agent_id: str | None = None,
    ) -> list[Any]:
        """Build cron tools."""
        sid, aid = _normalize_tenant_scope(service_id, tenant_agent_id)
        if service_id is None and tenant_agent_id is None:
            sid, aid = _tenant_scope_from_context(context)

        backend = self.get_backend(service_id=sid, agent_id=aid)
        if backend is None:
            logger.warning("[CronRuntimeBridge] cron backend is not ready, skip builtin cron tools")
            return []

        logger.info(
            "[CronRuntimeBridge] Building cron tools for scope=%s tool_scope=%s",
            (sid, aid),
            getattr(context, "tool_scope", "unknown"),
        )
        self.ensure_scheduler_started(service_id=sid, agent_id=aid)
        if context is not None:
            backend = _ContextInjectingCronBackend(backend, context)
        tools = create_cron_tools(
            backend,
            context=context,
            target_channels=[channel.value for channel in CronTargetChannel],
            default_target_channel=None,
            agent_id=agent_id,
            language=language,
        )
        logger.info(
            "[CronRuntimeBridge] Built %d cron tools: %s",
            len(tools),
            [tool.card.name if hasattr(tool, "card") else str(tool) for tool in tools],
        )
        return tools
