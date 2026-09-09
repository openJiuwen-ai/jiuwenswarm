from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from typing import Any, Optional

from openjiuwen.harness.tools.cron import CronToolBackend, CronToolContext, create_cron_tools

from jiuwenswarm.gateway.cron import CronTargetChannel
from jiuwenswarm.gateway.cron.dingtalk_routing import (
    build_dingtalk_cron_session_id_from_context,
    dingtalk_chat_type_from_metadata,
)
from jiuwenswarm.gateway.cron.models import (
    CRON_JOB_DEFAULT_MODE,
    coerce_cron_job_mode,
    is_cron_job_mode,
    is_valid_target_channel_id,
    normalize_target_channel_id,
)
from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import CronToolRoute, CronTools
from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools.check_plugin_privilege_tool import (
    INTENT_PERMISSION_MAP,
    ensure_plugin_privilege_granted,
    execute_plugin_privilege_check,
)
from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools.device_tool_planner import (
    CronDeviceToolPlanner,
)
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.common.schema.message import Message, ReqMethod
from jiuwenswarm.common.utils import logger


class _CronToolsCronBackend(CronToolBackend):
    """Adapt AgentServer CronTools to the DeepAgents CronToolBackend interface."""

    def __init__(
        self,
        cron_tools: CronTools,
        message_handler: MessageHandler | None = None,
        device_tool_planner: CronDeviceToolPlanner | None = None,
    ) -> None:
        self._cron_tools = cron_tools
        self._message_handler = message_handler
        self._device_tool_planner = (
            device_tool_planner or CronDeviceToolPlanner()
        )

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
        return CronToolRoute(
            request_id=request_id,
            channel_id=channel_id,
            session_id=session_id,
            chat_type=chat_type,
            project_dir=project_dir,
            project_id=project_id,
            work_mode=work_mode,
            app_id=app_id,
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
        # 兜底防线：当前请求若由调度器发起（scheduler._run_agent 会把
        # {"cron": {"job_id", "run_id"}} 放进 request metadata，经
        # _bind_runtime_cron_context 写入工具上下文），说明这是定时任务的执行
        # 现场。任务描述常含"每天/每周…"等字样，模型若还能调到 cron_create_job
        # （注册守卫失效或历史消息残留调用）会把调度再建一遍，这里直接拒绝，
        # 把模型引回"完成当前任务内容"。
        metadata = getattr(context, "metadata", None) if context is not None else None
        cron_meta = metadata.get("cron") if isinstance(metadata, dict) else None
        if isinstance(cron_meta, dict) and (cron_meta.get("job_id") or cron_meta.get("run_id")):
            job_id = str(cron_meta.get("job_id") or "")
            run_id = str(cron_meta.get("run_id") or "")
            logger.warning(
                "[CronRuntimeBridge] create_job rejected inside cron run: job_id=%s run_id=%s",
                job_id,
                run_id,
            )
            raise ValueError(
                f"当前请求是定时任务的调度执行现场 (job_id={job_id}, run_id={run_id})，"
                "禁止在执行过程中创建新的定时任务：该调度已存在，请直接完成当前任务内容；"
                "如需创建新的定时任务，请让用户在普通对话中发起。"
            )
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
        await _run_xiaoyi_privilege_preflight(
            payload,
            context=context,
            planner=self._device_tool_planner,
        )
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
                    from jiuwenswarm.gateway.cron.cron_expr import iso_to_five_field_cron
                    cron_expr = iso_to_five_field_cron(at_raw, timezone=timezone)
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
        if kind == "at":
            # kind=at 为一次性任务：5 段 cron 无年份字段，必须触发后删除，否则次年同期重复（对齐 controller.py kind=at 处理）
            out["delete_after_run"] = True
        elif kind == "cron" and cron_expr:
            # kind=cron 为周期/间隔任务：清除一次性语义，否则遗留的 delete_after_run
            # 会在首次触发后把任务标记为禁用/过期（对齐 controller.py update_job_a2a 处理）
            out["delete_after_run"] = False
        required_device_intents = (
            data.get("required_device_intents")
            if "required_device_intents" in data
            else payload_block.get("required_device_intents")
        )
        if required_device_intents is not None:
            out["required_device_intents"] = required_device_intents

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
        payload_mode = data.get("mode")
        if is_cron_job_mode(context_mode):
            mode_resolved = context_mode
        elif is_cron_job_mode(payload_mode):
            mode_resolved = payload_mode
        else:
            # profile modes (design / code.normal) and unknown values are not
            # cron execution modes; persist the default agent runner.
            mode_resolved = CRON_JOB_DEFAULT_MODE
        out["mode"] = coerce_cron_job_mode(mode_resolved, default=CRON_JOB_DEFAULT_MODE)
        return _attach_xiaoyi_device_route(out, context=context)

    return _attach_xiaoyi_device_route(data, context=context)


def _attach_xiaoyi_device_route(
    payload: dict[str, Any],
    *,
    context: CronToolContext | None,
) -> dict[str, Any]:
    out = dict(payload or {})
    raw_intents = out.get("required_device_intents")
    has_device_intents = isinstance(raw_intents, list) and any(
        str(item or "").strip() for item in raw_intents
    )
    context_channel = str((context.channel_id if context else "") or "").strip()
    if not has_device_intents:
        out.pop("xiaoyi_push_id", None)
        return out

    if context_channel != "xiaoyi":
        raise ValueError(
            "Xiaoyi device cron jobs must be created from an active Xiaoyi request"
        )

    metadata = context.metadata if context and isinstance(context.metadata, dict) else {}
    push_id = str(metadata.get("xiaoyi_push_id") or "").strip()
    if not push_id:
        raise ValueError(
            "Current Xiaoyi request does not contain push_id; "
            "cannot create a device cron job"
        )
    out["xiaoyi_push_id"] = push_id
    logger.info(
        "[CRON_DEVICE] phase=CRON_ROUTE_ATTACHED intent_count=%s push_id_present=true",
        len(raw_intents) if isinstance(raw_intents, list) else 0,
    )
    return out


async def _run_xiaoyi_privilege_preflight(
    payload: dict[str, Any],
    *,
    context: CronToolContext | None,
    planner: CronDeviceToolPlanner,
) -> None:
    context_channel = str((context.channel_id if context else "") or "").strip()
    if context_channel != "xiaoyi":
        payload.pop("required_device_intents", None)
        payload.pop("xiaoyi_push_id", None)
        return

    metadata = context.metadata if context and isinstance(context.metadata, dict) else {}
    request_id = str(metadata.get("request_id") or "").strip()
    job_key = str(payload.get("id") or payload.get("name") or "").strip()
    plan = await planner.plan(
        name=str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
        privilege_intents=frozenset(INTENT_PERMISSION_MAP),
        request_id=request_id,
        job_key=job_key,
    )
    payload["required_device_intents"] = list(plan.allowed_intents)
    if not plan.is_device_task:
        payload.pop("xiaoyi_push_id", None)
        return

    routed_payload = _attach_xiaoyi_device_route(payload, context=context)
    payload.clear()
    payload.update(routed_payload)
    logger.info(
        "[CRON_DEVICE] phase=DEVICE_TOOL_MAPPED request_id=%s job_key=%s "
        "tool_names=%s allowed_intents=%s privilege_intents=%s",
        request_id,
        job_key,
        list(plan.tool_names),
        list(plan.allowed_intents),
        list(plan.privilege_intents),
    )
    if plan.privilege_intents:
        logger.info(
            "[CRON_DEVICE] phase=PRIVILEGE_PREFLIGHT_BEGIN request_id=%s "
            "job_key=%s intent_count=%s",
            request_id,
            job_key,
            len(plan.privilege_intents),
        )
    for intent in plan.privilege_intents:
        logger.info(
            "[CRON_DEVICE] phase=PRIVILEGE_INTENT_BEGIN request_id=%s "
            "job_key=%s intent_name=%s",
            request_id,
            job_key,
            intent,
        )
        try:
            outputs = await execute_plugin_privilege_check(intent)
            ensure_plugin_privilege_granted(intent, outputs)
        except Exception as exc:
            logger.warning(
                "[CRON_DEVICE] phase=PRIVILEGE_INTENT_DENIED request_id=%s "
                "job_key=%s intent_name=%s error_type=%s error=%s",
                request_id,
                job_key,
                intent,
                type(exc).__name__,
                exc,
            )
            raise
        logger.info(
            "[CRON_DEVICE] phase=PRIVILEGE_INTENT_DONE request_id=%s "
            "job_key=%s intent_name=%s",
            request_id,
            job_key,
            intent,
        )
    logger.info(
        "[CRON_DEVICE] phase=CRON_CREATE_AFTER_PRIVILEGE request_id=%s "
        "job_key=%s intent_count=%s",
        request_id,
        job_key,
        len(plan.allowed_intents),
    )


def _add_xiaoyi_device_fields_to_cron_tools(tools: list[Any]) -> None:
    """Compatibility hook retained after restoring the upstream Cron schema."""
    _ = tools


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

    def build_tools(self, *, context: Any, agent_id: Optional[str], language: str = "cn") -> list[Any]:
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
            agent_id=agent_id,
            language=language,
        )
        _add_xiaoyi_device_fields_to_cron_tools(tools)
        logger.info("[CronRuntimeBridge] Built %d cron tools: %s", 
                    len(tools), 
                    [tool.card.name if hasattr(tool, 'card') else str(tool) for tool in tools])
        return tools
