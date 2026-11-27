from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from typing import Any, Optional

from openjiuwen.harness.tools.cron import CronToolBackend, CronToolContext, create_cron_tools

from jiuwenclaw.gateway.cron import CronTargetChannel
from jiuwenclaw.gateway.cron.models import is_valid_target_channel_id, normalize_target_channel_id
from jiuwenclaw.agentserver.tools.cron_tools import CronToolRoute, CronTools
from jiuwenclaw.gateway.message_handler import MessageHandler
from jiuwenclaw.schema.message import Message, ReqMethod
from jiuwenclaw.utils import logger


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
        return CronToolRoute(
            request_id=request_id,
            channel_id=channel_id,
            session_id=session_id,
            chat_type=chat_type,
        )

    async def list_jobs(self, *, include_disabled: bool = True) -> list[dict[str, Any]]:
        jobs = await self._cron_tools.list_jobs()
        rows = [self._to_backend_job(job) for job in jobs]
        if include_disabled:
            return rows
        return [job for job in rows if job.get("enabled", True)]

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = await self._cron_tools.get_job(job_id)
        if job is None:
            return None
        return self._to_backend_job(job)

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
        token = self._cron_tools.push_cron_route(self._route_from_context(context))
        try:
            job = await self._cron_tools.create_job(payload)
        finally:
            self._cron_tools.reset_cron_route(token)
        return self._to_backend_job(job)

    async def update_job(
        self,
        job_id: str,
        patch: dict[str, Any],
        *,
        context: CronToolContext | None = None,
    ) -> dict[str, Any]:
        payload = _extract_legacy_params(dict(patch or {}), context=context, require_schedule=False)
        token = self._cron_tools.push_cron_route(self._route_from_context(context))
        try:
            job = await self._cron_tools.update_job(job_id, payload)
        finally:
            self._cron_tools.reset_cron_route(token)
        return self._to_backend_job(job)

    async def delete_job(self, job_id: str) -> bool:
        return bool(await self._cron_tools.delete_job(job_id))

    async def toggle_job(self, job_id: str, enabled: bool) -> dict[str, Any]:
        job = await self._cron_tools.toggle_job(job_id, enabled)
        return self._to_backend_job(job)

    async def preview_job(self, job_id: str, count: int = 5) -> list[dict[str, Any]]:
        rows = await self._cron_tools.preview_job(job_id, count)
        return list(rows or [])

    async def run_now(self, job_id: str) -> str:
        token = self._cron_tools.push_cron_route(CronToolRoute())
        try:
            run_result = await self._cron_tools.run_now(job_id)
        finally:
            self._cron_tools.reset_cron_route(token)
        if isinstance(run_result, dict):
            return str(run_result.get("run_id") or "")
        return str(run_result or "")

    async def status(self) -> dict[str, Any]:
        jobs = await self._cron_tools.list_jobs()
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
                "mode": (mode or context.mode or "agent.fast"),
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
                "channel": str(row.get("targets") or CronTargetChannel.WEB.value).strip() or CronTargetChannel.WEB.value,
            },
        )
        row.setdefault("session_target", "isolated")
        row.setdefault("compat_mode", "legacy")
        return row


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
        if kind and kind != "cron":
            raise ValueError("Only cron schedule is supported by the current gateway bridge")

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

        payload_block = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        payload_kind = str(payload_block.get("kind") or "agentTurn").strip()
        if payload_kind and payload_kind != "agentTurn":
            raise ValueError("Only agentTurn cron jobs are supported by the current gateway bridge")
        description = str(
            payload_block.get("message")
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

        context_mode = getattr(context, "mode", None)
        mode_resolved = (
            context_mode
            or data.get("mode")
            or "agent.fast"
        )
        out["mode"] = (
            str(mode_resolved).strip().lower()
            if isinstance(mode_resolved, str) and str(mode_resolved).strip()
            else "agent.fast"
        )
        return out

    return data


class CronRuntimeBridge:
    """Resolve the host cron backend for DeepAgents while keeping gateway diffs minimal."""

    def __init__(self) -> None:
        self._backend_override: CronToolBackend | None = None
        self._resolved_backend: CronToolBackend | None = None

    def set_backend(self, backend: CronToolBackend | None) -> None:
        self._backend_override = backend
        self._resolved_backend = backend

    def get_backend(self) -> CronToolBackend | None:
        if self._backend_override is not None:
            return self._backend_override
        if self._resolved_backend is not None:
            return self._resolved_backend

        message_handler = None
        try:
            message_handler = MessageHandler.get_instance()
        except RuntimeError:
            message_handler = None

        backend: CronToolBackend = _CronToolsCronBackend(CronTools(), message_handler=message_handler)
        self._resolved_backend = backend
        logger.info("[CronRuntimeBridge] CronTools backend initialized successfully")
        return backend

    def ensure_scheduler_started(self) -> None:
        """确保scheduler已启动，如果未启动则异步启动"""
        backend = self.get_backend()
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

    def build_tools(self, *, context: Any, agent_id: Optional[str]) -> list[Any]:
        """Build cron tools."""
        backend = self.get_backend()
        if backend is None:
            logger.warning("[CronRuntimeBridge] cron backend is not ready, skip builtin cron tools")
            return []
        
        logger.info("[CronRuntimeBridge] Building cron tools for context: %s", 
                    getattr(context, 'tool_scope', 'unknown'))
        tools = create_cron_tools(
            backend,
            context=context,
            target_channels=[channel.value for channel in CronTargetChannel],
            default_target_channel=None,
            agent_id=agent_id
        )
        logger.info("[CronRuntimeBridge] Built %d cron tools: %s", 
                    len(tools), 
                    [tool.card.name if hasattr(tool, 'card') else str(tool) for tool in tools])
        return tools