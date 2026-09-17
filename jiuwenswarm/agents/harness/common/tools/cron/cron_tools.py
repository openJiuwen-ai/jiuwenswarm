from __future__ import annotations

import asyncio
import contextvars
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard
from jiuwenswarm.gateway.cron.cron_expr import normalize_cron_expr
from jiuwenswarm.gateway.cron.store import CronJobStore, _PROACTIVE_TICK_MODE
from jiuwenswarm.gateway.cron.scheduler import _cron_next_push_dt, CronSchedulerService
from jiuwenswarm.gateway.cron.models import (
    CronTargetChannel,
    cron_job_modes_for_tools,
    is_valid_target_channel_id,
    normalize_cron_job_mode,
    normalize_target_channel_id,
    validate_cron_model,
)
from jiuwenswarm.server.gateway_push import (
    GatewayPushTransport,
    WebSocketGatewayPushTransport,
)
from jiuwenswarm.common.utils import get_cron_jobs_path

logger = logging.getLogger(__name__)

# 按 asyncio Task 隔离：多 session 并发时不能用单例字段存路由，否则后到的请求会覆盖先到的 session_id。
_cron_route_ctx: contextvars.ContextVar[CronToolRoute | None] = contextvars.ContextVar(
    "jiuwenswarm_cron_route", default=None
)


@dataclass(frozen=True, slots=True)
class CronToolRoute:
    """当前请求同步到 Gateway 时使用的路由（request_id / channel / session / chat_type / app_id）。"""

    request_id: str = ""
    channel_id: str = CronTargetChannel.WEB.value
    session_id: str | None = None
    chat_type: str | None = None  # "group" 表示群聊, "p2p" 或 None 表示私聊
    app_id: str = ""
    project_dir: str = ""  # 当前 agent 工作目录，用于 cron 任务归属项目解析
    project_id: str = ""  # 当前会话锁定项目 ID；优先于 project_dir，避免同路径双模式误判
    work_mode: str = ""  # 当前会话锁定工作模式，用于 project_dir 归属消歧


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
            path=get_cron_jobs_path()
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
            from jiuwenswarm.gateway.routing.agent_client import AgentServerClient
            
            agent_client = self._agent_client
            message_handler = self._message_handler
            
            # If not provided, try to get from singletons
            if agent_client is None:
                try:
                    agent_client = AgentServerClient.get_instance()
                except (RuntimeError, AttributeError):
                    agent_client = None
            
            if message_handler is None:
                try:
                    from jiuwenswarm.gateway.message_handler import MessageHandler
                    message_handler = MessageHandler.get_instance()
                except RuntimeError:
                    message_handler = None
            
            if agent_client is None:
                logger.warning("[CronTools] Cannot start scheduler: AgentServerClient not available")
                self._scheduler_started = True  # Mark as tried
                return None

            if message_handler is None:
                logger.warning("[CronTools] Cannot start scheduler: MessageHandler not available")
                self._scheduler_started = True
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
        from jiuwenswarm.common.e2a.constants import E2A_RESPONSE_KIND_CRON

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
        if channel.startswith("wechat"):
            return CronTargetChannel.WECHAT.value
        if channel.startswith("dingtalk"):
            return CronTargetChannel.DINGTALK.value
        if channel.startswith("tui"):
            return CronTargetChannel.TUI.value

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

    @staticmethod
    def _resolve_work_mode_from_params(
        params: dict[str, Any],
        *,
        channel_id: str = "",
    ) -> tuple[str, str | None]:
        """从请求参数解析 work_mode(严格校验)。

        与 ``CronController.create_job`` 保持一致:非法值返回 BAD_REQUEST,
        由调用方决定如何处理。

        Returns:
            ``(work_mode, error_code)``:成功时 ``error_code`` 为 ``None``,
            失败时 ``work_mode`` 为空串。
        """
        from jiuwenswarm.server.runtime.session.work_mode import resolve_request_work_mode

        work_mode, mode_err = resolve_request_work_mode(params, channel_id)
        if mode_err is not None:
            return "", mode_err
        return work_mode, None

    @staticmethod
    def _sync_patch_payload(patch: dict[str, Any]) -> dict[str, Any]:
        payload = {k: v for k, v in patch.items() if k != "project_dir"}
        if "model_name" in payload:
            payload["model_name"] = payload["model_name"] or ""
        return payload

    async def list_jobs(self) -> Any:
        jobs = await self._local_store.list_jobs()
        # 给受保护的 proactive.tick job 标记 protected，让 LLM 在批量操作时
        # （如"删除所有定时任务"）能识别并优雅跳过，而不是删到一半才遇错。
        out = []
        for j in jobs:
            d = j.to_dict()
            if str(d.get("mode") or "").strip().lower() == _PROACTIVE_TICK_MODE:
                d["protected"] = True
                d["protected_reason"] = (
                    "由主动推荐开关自动维护，不可删除/启停；如需关闭请到设置→主动推荐关闭开关。"
                )
            out.append(d)
        return out

    async def get_job(self, job_id: str) -> Any:
        job = await self._local_store.get_job(job_id)
        return job.to_dict() if job else None

    async def create_job(self, params: dict[str, Any]) -> Any:
        normalized = dict(params or {})
        normalized.pop("session_id", None)
        normalized["targets"] = self._normalize_targets_param(normalized.get("targets"))
        normalized["cron_expr"] = normalize_cron_expr(str(normalized.get("cron_expr") or "").strip())
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
        r = self._route()
        sid = r.session_id
        if isinstance(sid, str) and sid.strip():
            session_kw["session_id"] = sid.strip()
        chat_type = r.chat_type
        if chat_type:
            session_kw["chat_type"] = chat_type
        app_id = str(getattr(r, "app_id", None) or "").strip()
        if app_id:
            session_kw["app_id"] = app_id
        mode_kw: dict[str, Any] = {}
        mode_raw = normalized.get("mode")
        if mode_raw is not None and str(mode_raw).strip():
            mode_kw["mode"] = normalize_cron_job_mode(mode_raw)
        model_kw: dict[str, Any] = {}
        model_name_raw = normalized.get("model_name")
        if model_name_raw is not None and str(model_name_raw).strip():
            model_kw["model_name"] = validate_cron_model(model_name_raw)
        # project_dir -> project_id follows the same rules as the gateway controller.
        # 用 key presence 区分「未传」和「显式空串」：显式传 "" 归默认项目，
        # 未传时从 route 上下文取 project_dir（设计文档 §5.1）。
        if "project_dir" in normalized:
            project_dir_val = str(normalized.get("project_dir") or "").strip()
        else:
            project_dir_val = str(self._route().project_dir or "").strip()

        # work_mode 解析(严格校验,与 CronController.create_job 保持一致)
        route_work_mode = str(r.work_mode or "").strip()
        if "work_mode" not in normalized and route_work_mode:
            normalized["work_mode"] = route_work_mode
        channel_id_val = self._resolve_channel_id() or "web"
        work_mode, mode_err = self._resolve_work_mode_from_params(
            normalized, channel_id=channel_id_val,
        )
        if mode_err is not None:
            raise ValueError(f"invalid work_mode: {normalized.get('work_mode')!r}")

        # 优先接受显式 project_id(修改计划 §5 链路 B):
        # 1. 真实 project_id 命中 → 从 Project 记录注入精确 work_mode
        # 2. 默认项目 / 不存在 → 按 (work_mode, project_dir) 解析
        from jiuwenswarm.server.runtime.session.project_store import resolve_cron_project_binding

        raw_project_id = str(normalized.get("project_id") or "").strip()
        if not raw_project_id:
            raw_project_id = str(r.project_id or "").strip()
        binding = resolve_cron_project_binding(raw_project_id, project_dir_val, work_mode)
        if binding.error is not None:
            if binding.hidden:
                raise ValueError(f"project not found: {raw_project_id!r}")
            raise ValueError(binding.error)
        resolved_project_id = binding.project_id
        work_mode = binding.work_mode

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
            project_id=resolved_project_id,
            work_mode=work_mode,
            user_id=str(normalized.get("user_id") or "").strip(),
            **session_kw,
            **mode_kw,
            **model_kw,
        )
        try:
            sync_payload = job.to_dict()
            sync_payload["project_dir"] = project_dir_val
            await self._send("create", sync_payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[CronTools] sync create to gateway failed: %s", exc)
        
        # Reload scheduler to pick up the new job
        await self._reload_scheduler()
        
        return job.to_dict()

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> Any:
        normalized_patch = dict(patch or {})
        normalized_patch.pop("session_id", None)
        if "cron_expr" in normalized_patch:
            normalized_patch["cron_expr"] = normalize_cron_expr(str(normalized_patch["cron_expr"]).strip())
        if "targets" in normalized_patch:
            normalized_patch["targets"] = self._normalize_targets_param(normalized_patch.get("targets"))
            t = str(normalized_patch.get("targets") or "").strip()
            if t.startswith("feishu_enterprise:"):
                sid = self._route().session_id
                if isinstance(sid, str) and sid.strip():
                    normalized_patch["session_id"] = sid.strip()
            else:
                normalized_patch["session_id"] = None
        if "mode" in normalized_patch:
            normalized_patch["mode"] = normalize_cron_job_mode(normalized_patch.get("mode"))
        if "model_name" in normalized_patch:
            normalized_patch["model_name"] = validate_cron_model(normalized_patch.get("model_name"))

        # work_mode / project_id / project_dir 重解析(共享 helper):
        # 与 CronController.update_job 共用同一 ``resolve_cron_job_patch``,
        # 确保 AgentTool 与 Web RPC 两条链路逻辑一致。
        existing = await self._local_store.get_job(job_id)
        if existing is None:
            raise KeyError("job not found")

        channel_id_val = self._resolve_channel_id() or "web"
        from jiuwenswarm.server.runtime.session.project_store import resolve_cron_job_patch
        resolve_cron_job_patch(
            normalized_patch,
            existing_work_mode=existing.work_mode or "",
            resolve_work_mode_fn=self._resolve_work_mode_from_params,
            channel_id=channel_id_val,
        )

        # 仅在 patch 包含 session_id 或 targets 时才更新 chat_type
        # (与 CronController.update_job 一致),避免无关更新静默覆盖推送路由
        if "session_id" in normalized_patch or "targets" in normalized_patch:
            chat_type = self._route().chat_type
            normalized_patch["chat_type"] = chat_type if chat_type else None
        job = await self._local_store.update_job(job_id, normalized_patch)
        try:
            await self._send(
                "update",
                {"job_id": job_id, "patch": self._sync_patch_payload(normalized_patch)},
            )
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
        # proactive.tick job 的开关由 config 的 proactive_recommendation.enabled 驱动，
        # 禁止手动 toggle——否则会与 config 开关不一致。引导用户去设置关开关。
        existing = await self._local_store.get_job(job_id)
        if existing is not None and str(getattr(existing, "mode", "") or "").strip().lower() == "proactive.tick":
            raise RuntimeError(
                "主动推荐定时任务由设置→主动推荐开关控制，不能手动启停；请到设置→主动推荐操作。"
            )
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
            if out and push_dt.isoformat() == out[-1]["push_at"]:
                break
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
        mode = kwargs.get("mode")
        if mode is not None and str(mode).strip():
            params["mode"] = mode
        model_name = kwargs.get("model_name")
        if model_name is not None and str(model_name).strip():
            params["model_name"] = model_name
        if "project_dir" in kwargs and kwargs.get("project_dir") is not None:
            params["project_dir"] = str(kwargs.get("project_dir") or "").strip()
        if "project_id" in kwargs and kwargs.get("project_id") is not None:
            params["project_id"] = str(kwargs.get("project_id") or "").strip()
        if "work_mode" in kwargs and kwargs.get("work_mode") is not None:
            params["work_mode"] = str(kwargs.get("work_mode") or "").strip()
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
                        "mode": {
                            "type": "string",
                            "enum": cron_job_modes_for_tools(),
                            "description": (
                                "Agent runtime mode when the job runs "
                                "(agent, team, ...). Default: agent."
                            ),
                        },
                        "model_name": {
                            "type": "string",
                            "description": "Model name or alias to use. Omit for default.",
                        },
                        "project_dir": {
                            "type": "string",
                            "description": "Absolute path to the project directory. \
                                Omit for current session's project.",
                        },
                        "project_id": {
                            "type": "string",
                            "description": (
                                "Explicit project id (takes priority over project_dir). "
                                "Omit to resolve from project_dir + work_mode."
                            ),
                        },
                        "work_mode": {
                            "type": "string",
                            "enum": ["code", "work"],
                            "description": (
                                "Working mode of the target project (code/work). "
                                "Defaults to current channel default (tui->code, web->work). "
                                "Only used when project_id is not provided; ignored if project_id "
                                "is provided (work_mode inherited from the project)."
                            ),
                        },
                    },
                    "required": ["name", "cron_expr", "timezone", "description"],
                },
                func=self._create_job_tool,
            ),
            make_tool(
                name="cron_update_job",
                description=(
                    "Update an existing cron job. Pass job_id and a patch dict with fields to update "
                    "(name, enabled, cron_expr, timezone, description, wake_offset_seconds, "
                    "targets, mode, model_name, project_dir, project_id)."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "Job id to update"},
                        "patch": {
                            "type": "object",
                            "description": (
                                "Fields to update (name, enabled, cron_expr, timezone, "
                                "description, wake_offset_seconds, targets, mode, model_name, "
                                "project_dir, project_id). work_mode is not accepted as an "
                                "independent patch field; to change work_mode, patch project_id "
                                "or project_dir + work_mode (work_mode only disambiguates the "
                                "target project when resolving project_dir)."
                            ),
                            "properties": {
                                "name": {"type": "string"},
                                "enabled": {"type": "boolean"},
                                "cron_expr": {"type": "string"},
                                "timezone": {"type": "string"},
                                "description": {"type": "string"},
                                "wake_offset_seconds": {"type": "integer"},
                                "delete_after_run": {"type": "boolean"},
                                "targets": {
                                    "type": "string",
                                    "enum": [e.value for e in CronTargetChannel],
                                    "description": (
                                        "推送频道：web/tui/feishu/dingtalk/whatsapp/wecom/xiaoyi/wechat"
                                    ),
                                },
                                "mode": {
                                    "type": "string",
                                    "enum": cron_job_modes_for_tools(),
                                    "description": "Agent runtime mode (agent, team, ...)",
                                },
                                "model_name": {
                                    "type": "string",
                                    "description": "Model name or alias. Set to empty string to reset to default.",
                                },
                                "project_dir": {
                                    "type": "string",
                                    "description": (
                                        "Absolute path to the project directory. Set to empty "
                                        "string for default project. When set, project_id is "
                                        "re-resolved from (work_mode, project_dir)."
                                    ),
                                },
                                "project_id": {
                                    "type": "string",
                                    "description": (
                                        "Directly patch the project_id (takes priority over "
                                        "project_dir). work_mode is re-injected from the "
                                        "project record. Must reference an existing visible "
                                        "project or a default project (default / default_code)."
                                    ),
                                },
                                "work_mode": {
                                    "type": "string",
                                    "enum": ["code", "work"],
                                    "description": (
                                        "Disambiguates target project when patching "
                                        "project_dir. Ignored if project_id is patched "
                                        "directly. Not a standalone patchable field."
                                    ),
                                },
                            },
                        },
                    },
                    "required": ["job_id", "patch"],
                },
                func=self._update_job_tool,
            ),
            make_tool(
                name="cron_delete_job",
                description=(
                    "Delete cron job by id. "
                    "Note: jobs with protected=true (from cron_list_jobs) are managed by "
                    "system config and cannot be deleted here; tell the user to toggle the "
                    "corresponding config switch instead."
                ),
                input_params={"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
                func=self.delete_job,
            ),
            make_tool(
                name="cron_toggle_job",
                description=(
                    "Enable or disable cron job. "
                    "Note: jobs with protected=true cannot be toggled here; they are driven by "
                    "system config."
                ),
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
