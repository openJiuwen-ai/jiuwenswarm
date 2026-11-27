from __future__ import annotations

import asyncio
import contextvars
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard
from jiuwenclaw.gateway.cron.store import CronJobStore
from jiuwenclaw.gateway.cron.scheduler import _cron_next_push_dt, CronSchedulerService
from jiuwenclaw.gateway.cron.models import (
    CronTargetChannel,
    is_valid_target_channel_id,
    normalize_target_channel_id,
)
from jiuwenclaw.agentserver.gateway_push import (
    GatewayPushTransport,
    WebSocketGatewayPushTransport,
)
from jiuwenclaw.utils import get_user_workspace_dir

logger = logging.getLogger(__name__)

# 按 asyncio Task 隔离：多 session 并发时不能用单例字段存路由，否则后到的请求会覆盖先到的 session_id。
_cron_route_ctx: contextvars.ContextVar[CronToolRoute | None] = contextvars.ContextVar(
    "jiuwenclaw_cron_route", default=None
)


@dataclass(frozen=True, slots=True)
class CronToolRoute:
    """当前请求同步到 Gateway 时使用的路由（request_id / channel / session / chat_type）。"""

    request_id: str = ""
    channel_id: str = CronTargetChannel.WEB.value
    session_id: str | None = None
    chat_type: str | None = None  # "group" 表示群聊, "p2p" 或 None 表示私聊


class CronTools:
    """Agent-side cron tools with local cron_jobs.json as source of truth.

    路由用 ContextVar 按 Task 隔离（与 interface 中 ``push_cron_route`` / ``reset_cron_route`` 配对）；
    同进程一套 LocalFunction，并发安全依赖当前 asyncio 任务的上下文而非单例可变字段。
    
    包含内置调度器，即使 Gateway 未启动也能执行定时任务。
    """

    def __init__(
        self,
        gateway_push: GatewayPushTransport | None = None,
        *,
        agent_client: Any | None = None,
        message_handler: Any | None = None,
    ) -> None:
        self._gateway_push: GatewayPushTransport = gateway_push or WebSocketGatewayPushTransport()
        self._local_store = CronJobStore(
            path=get_user_workspace_dir() / "agent" / "home" / "cron_jobs.json"
        )
        # 内置调度器，用于在 Agent-side 执行定时任务
        self._scheduler: CronSchedulerService | None = None
        self._agent_client = agent_client
        self._message_handler = message_handler
        self._scheduler_started = False

    async def ensure_scheduler(self) -> CronSchedulerService | None:
        """Ensure the scheduler is started."""
        if self._scheduler is not None and self._scheduler.is_running():
            return self._scheduler
        
        if self._scheduler_started:
            # Already tried to start but failed or stopped
            return self._scheduler
        
        # Try to create and start scheduler
        try:
            # Lazy import to avoid circular dependency
            from jiuwenclaw.gateway.agent_client import AgentServerClient
            
            agent_client = self._agent_client
            message_handler = self._message_handler
            
            # If not provided, try to get from singletons
            if agent_client is None:
                try:
                    agent_client = AgentServerClient.get_instance()
                except RuntimeError:
                    agent_client = None
            
            if message_handler is None:
                try:
                    from jiuwenclaw.gateway.message_handler import MessageHandler
                    message_handler = MessageHandler.get_instance()
                except RuntimeError:
                    message_handler = None
            
            if agent_client is None:
                logger.warning("[CronTools] Cannot start scheduler: AgentServerClient not available")
                self._scheduler_started = True  # Mark as tried
                return None
            
            self._scheduler = CronSchedulerService(
                store=self._local_store,
                agent_client=agent_client,
                message_handler=message_handler,
            )
            await self._scheduler.start()
            logger.info("[CronTools] Scheduler started successfully")
            self._scheduler_started = True
            return self._scheduler
            
        except Exception as exc:
            logger.warning("[CronTools] Failed to start scheduler: %s", exc)
            self._scheduler_started = True  # Mark as tried
            return None

    async def _reload_scheduler(self) -> None:
        """Reload scheduler if it's running."""
        scheduler = await self.ensure_scheduler()
        if scheduler is not None:
            try:
                await scheduler.reload()
                logger.debug("[CronTools] Scheduler reloaded")
            except Exception as exc:
                logger.warning("[CronTools] Failed to reload scheduler: %s", exc)

    @staticmethod
    def push_cron_route(route: CronToolRoute) -> contextvars.Token:
        """进入一轮 Agent 执行前调用；须与 ``reset_cron_route`` 配对（通常在 finally 中）。"""
        return _cron_route_ctx.set(route)

    @staticmethod
    def reset_cron_route(token: contextvars.Token) -> None:
        _cron_route_ctx.reset(token)

    @staticmethod
    def _route() -> CronToolRoute:
        r = _cron_route_ctx.get()
        return r if r is not None else CronToolRoute()

    async def _send_split(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        from jiuwenclaw.e2a.constants import E2A_RESPONSE_KIND_CRON

        r = self._route()
        payload = {
            "request_id": r.request_id,
            "channel_id": r.channel_id,
            "session_id": r.session_id,
            "response_kind": E2A_RESPONSE_KIND_CRON,
            "body": {
                "action": action,
                "status": "ok",
                "data": dict(params or {}),
                "message": "",
            },
        }
        await self._gateway_push.send_push(payload)
        return {"action": action, "status": "forwarded", "data": None, "message": "cron request forwarded to gateway"}

    async def _send(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._send_split(action, params)

    @staticmethod
    def _is_valid_target(value: str) -> bool:
        return is_valid_target_channel_id(value)

    def _default_target_from_channel(self) -> str:
        channel_raw = self._resolve_channel_id()
        channel = channel_raw.lower()
        if channel.startswith("feishu_enterprise:"):
            return normalize_target_channel_id(channel_raw, default=CronTargetChannel.WEB.value)
        if channel.startswith("feishu"):
            return CronTargetChannel.FEISHU.value
        if channel.startswith("wecom"):
            return CronTargetChannel.WECOM.value
        if channel.startswith("xiaoyi"):
            return CronTargetChannel.XIAOYI.value
        if channel.startswith("whatsapp"):
            return CronTargetChannel.WHATSAPP.value
        return CronTargetChannel.WEB.value

    def _resolve_channel_id(self) -> str:
        r = self._route()
        channel_raw = str(r.channel_id or "").strip()
        if channel_raw:
            return channel_raw
        request_id = str(r.request_id or "").strip()
        if ":" not in request_id:
            return ""
        return request_id.rsplit(":", 1)[0].strip()

    def _normalize_targets_param(self, raw: Any) -> str:
        target = str(raw or "").strip()
        if self._is_valid_target(target):
            normalized = normalize_target_channel_id(target, default=CronTargetChannel.WEB.value)
            logger.info(
                "[CronTools] normalize targets from explicit value: raw=%s normalized=%s route_channel=%s",
                target,
                normalized,
                self._route().channel_id,
            )
            return normalized
        fallback = self._default_target_from_channel()
        logger.info(
            "[CronTools] normalize targets from fallback: raw=%s fallback=%s route_channel=%s request_id=%s",
            target,
            fallback,
            self._route().channel_id,
            self._route().request_id,
        )
        return fallback

    async def list_jobs(self) -> Any:
        jobs = await self._local_store.list_jobs()
        return [j.to_dict() for j in jobs]

    async def get_job(self, job_id: str) -> Any:
        job = await self._local_store.get_job(job_id)
        return job.to_dict() if job else None

    async def create_job(self, params: dict[str, Any]) -> Any:
        normalized = dict(params or {})
        normalized.pop("session_id", None)
        normalized["targets"] = self._normalize_targets_param(normalized.get("targets"))
        targets_str = normalized["targets"]
        logger.info(
            "[CronTools] create_job: route(channel=%s session=%s request=%s) input.targets=%s normalized.targets=%s",
            self._route().channel_id,
            self._route().session_id,
            self._route().request_id,
            params.get("targets") if isinstance(params, dict) else None,
            targets_str,
        )
        session_kw: dict[str, Any] = {}
        if str(targets_str).strip().startswith("feishu_enterprise:"):
            sid = self._route().session_id
            if isinstance(sid, str) and sid.strip():
                session_kw["session_id"] = sid.strip()
        chat_type = self._route().chat_type
        if chat_type:
            session_kw["chat_type"] = chat_type
        job = await self._local_store.create_job(
            job_id=str(normalized.get("id") or "").strip() or None,
            name=str(normalized.get("name") or "").strip(),
            cron_expr=str(normalized.get("cron_expr") or "").strip(),
            timezone=str(normalized.get("timezone") or "Asia/Shanghai").strip() or "Asia/Shanghai",
            description=str(normalized.get("description") or ""),
            targets=targets_str,
            enabled=bool(normalized.get("enabled", True)),
            wake_offset_seconds=normalized.get("wake_offset_seconds"),
            delete_after_run=normalized.get("delete_after_run"),
            **session_kw,
        )
        try:
            await self._send("create", job.to_dict())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[CronTools] sync create to gateway failed: %s", exc)
        
        # Reload scheduler to pick up the new job
        await self._reload_scheduler()
        
        return job.to_dict()

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> Any:
        normalized_patch = dict(patch or {})
        normalized_patch.pop("session_id", None)
        if "targets" in normalized_patch:
            normalized_patch["targets"] = self._normalize_targets_param(normalized_patch.get("targets"))
            t = str(normalized_patch.get("targets") or "").strip()
            if t.startswith("feishu_enterprise:"):
                sid = self._route().session_id
                if isinstance(sid, str) and sid.strip():
                    normalized_patch["session_id"] = sid.strip()
            else:
                normalized_patch["session_id"] = None
        chat_type = self._route().chat_type
        normalized_patch["chat_type"] = chat_type if chat_type else None
        job = await self._local_store.update_job(job_id, normalized_patch)
        try:
            await self._send("update", {"job_id": job_id, "patch": normalized_patch})
        except Exception as exc:  # noqa: BLE001
            logger.warning("[CronTools] sync update to gateway failed: %s", exc)
        
        # Reload scheduler to pick up the changes
        await self._reload_scheduler()
        
        return job.to_dict()

    async def delete_job(self, job_id: str) -> Any:
        deleted = await self._local_store.delete_job(job_id)
        try:
            await self._send("delete", {"job_id": job_id})
        except Exception as exc:  # noqa: BLE001
            logger.warning("[CronTools] sync delete to gateway failed: %s", exc)
        
        # Reload scheduler to pick up the changes
        await self._reload_scheduler()
        
        return deleted

    async def toggle_job(self, job_id: str, enabled: bool) -> Any:
        job = await self._local_store.update_job(job_id, {"enabled": bool(enabled)})
        try:
            await self._send("toggle", {"job_id": job_id, "enabled": bool(enabled)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("[CronTools] sync toggle to gateway failed: %s", exc)
        
        # Reload scheduler to pick up the changes
        await self._reload_scheduler()
        
        return job.to_dict()

    async def preview_job(self, job_id: str, count: int = 5) -> Any:
        job = await self._local_store.get_job(job_id)
        if job is None:
            raise KeyError("job not found")
        count = max(1, min(int(count), 50))
        tz = ZoneInfo(job.timezone)
        base = datetime.now(tz=tz)
        out: list[dict[str, Any]] = []
        push_dt = base
        for _ in range(count):
            try:
                push_dt = _cron_next_push_dt(job.cron_expr, push_dt)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "CroniterBadDateError" in msg or "failed to find next date" in msg:
                    break
                raise
            wake_dt = push_dt - timedelta(seconds=max(0, int(job.wake_offset_seconds or 0)))
            out.append({"wake_at": wake_dt.isoformat(), "push_at": push_dt.isoformat()})
        return out

    async def run_now(self, job_id: str) -> Any:
        return await self._send("run_now", {"job_id": job_id})

    async def _create_job_tool(self, **kwargs: Any) -> Any:
        params: dict[str, Any] = {
            "name": kwargs.get("name"),
            "cron_expr": kwargs.get("cron_expr"),
            "timezone": kwargs.get("timezone"),
            "targets": kwargs.get("targets", ""),
            "enabled": kwargs.get("enabled", True),
            "description": kwargs.get("description"),
        }
        wake_offset_seconds = kwargs.get("wake_offset_seconds")
        if wake_offset_seconds is not None:
            params["wake_offset_seconds"] = wake_offset_seconds
        return await self.create_job(params)

    async def _update_job_tool(self, job_id: str, patch: dict[str, Any]) -> Any:
        return await self.update_job(job_id, patch)

    async def _preview_job_tool(self, job_id: str, count: int = 5) -> Any:
        return await self.preview_job(job_id, count)

    def get_tools(self) -> list[Tool]:
        def make_tool(name: str, description: str, input_params: dict, func) -> Tool:
            card = ToolCard(
                name=name,
                description=description,
                input_params=input_params,
            )
            return LocalFunction(card=card, func=func)

        return [
            make_tool(
                name="cron_list_jobs",
                description="List all cron jobs.",
                input_params={"type": "object", "properties": {}},
                func=self.list_jobs,
            ),
            make_tool(
                name="cron_get_job",
                description="Get a cron job by id.",
                input_params={
                    "type": "object",
                    "properties": {"job_id": {"type": "string"}},
                    "required": ["job_id"],
                },
                func=self.get_job,
            ),
            make_tool(
                name="cron_create_job",
                description="Create cron job.",
                input_params={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "cron_expr": {"type": "string"},
                        "timezone": {"type": "string"},
                        "description": {"type": "string"},
                        "targets": {"type": "string"},
                        "enabled": {"type": "boolean"},
                        "wake_offset_seconds": {"type": "integer"},
                    },
                    "required": ["name", "cron_expr", "timezone", "description"],
                },
                func=self._create_job_tool,
            ),
            make_tool(
                name="cron_update_job",
                description="Update cron job.",
                input_params={
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "patch": {"type": "object"},
                    },
                    "required": ["job_id", "patch"],
                },
                func=self._update_job_tool,
            ),
            make_tool(
                name="cron_delete_job",
                description="Delete cron job by id.",
                input_params={"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
                func=self.delete_job,
            ),
            make_tool(
                name="cron_toggle_job",
                description="Enable or disable cron job.",
                input_params={
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "enabled": {"type": "boolean"},
                    },
                    "required": ["job_id", "enabled"],
                },
                func=self.toggle_job,
            ),
            make_tool(
                name="cron_preview_job",
                description="Preview next runs.",
                input_params={
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "count": {"type": "integer"},
                    },
                    "required": ["job_id"],
                },
                func=self._preview_job_tool,
            ),
            make_tool(
                name="cron_run_now",
                description="Trigger run now.",
                input_params={"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
                func=self.run_now,
            ),
        ]
