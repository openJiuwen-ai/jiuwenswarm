from __future__ import annotations

import asyncio
import re
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
    is_valid_target_channel_id,
    normalize_target_channel_id,
)
from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import CronToolRoute, CronTools
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.common.schema.message import Message, ReqMethod
from jiuwenswarm.common.utils import logger


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

        # 从 context 取 user_id，agent 内部调 cron_create_job 时无 web 连接来源，
        # 靠 _bind_runtime_cron_context 从会话 metadata.user_id 注入。
        context_user_id = getattr(context, "user_id", None)
        if isinstance(context_user_id, str) and context_user_id.strip():
            out["user_id"] = context_user_id.strip()
            logger.info(
                "[CronRuntimeBridge] _extract_legacy_params: added user_id=%s from context",
                out["user_id"],
            )

        return out

    # 非 legacy 格式(直接传参): 同样从 context 注入 user_id
    _user_id = getattr(context, "user_id", None)
    if isinstance(_user_id, str) and _user_id.strip():
        if "user_id" not in data:
            data["user_id"] = _user_id.strip()
            logger.info(
                "[CronRuntimeBridge] _extract_legacy_params: added user_id=%s from context (flat params)",
                data["user_id"],
            )
    return data


# --- openjiuwen cron 工具描述中的 dow 语义修正 ---
# openjiuwen 的 cron 工具描述（openjiuwen/harness/prompts/tools/cron.py）把星期字段
# 声明为 Quartz 的 1=SUN...7=SAT，而 jiuwenswarm 后端调度（gateway/cron 用 croniter）
# 与前端 CronPanel 统一按 0=SUN...6=SAT 解析。LLM 照描述用数字 dow 生成时（例如把
# "周三、周五"写成 4,6），会被解析成周四、周六，整体偏移 +1 天（见 bugfix：
# cron-dow-schema-description）。这里在工具注册前对 ToolCard 的描述做精确替换修正，
# 并推荐字母缩写（WED/FRI）彻底消除歧义。
# 替换对以"核心子串"为锚点（不带行首装饰符），openjiuwen 改版导致失配时保留原文
# 并打 warning，避免静默失效。
_CRON_DOW_SEMANTIC_FIXES: tuple[tuple[str, str], ...] = (
    # cn：范围声明
    (
        "周(1-7或?)",
        "周(0-6或?，0=周日…6=周六；推荐用字母缩写 SUN/MON/…/SAT 避免歧义)",
    ),
    # cn：整除规则句（工具级描述与 cron_create_job 描述共用）
    (
        "周(1-7)：*/X 仅支持 X 整除7的值：1/7。1=SUN,7=SAT。",
        "周(0-6)：*/X 仅支持 X 整除7的值：1/7。编号0=周日…6=周六，"
        "与系统解析(croniter)一致，注意不是Quartz的1=SUN；推荐用字母缩写（如WED/FRI）避免歧义。",
    ),
    # cn：schedule.expr 字段描述（小写 c 开头）
    (
        "cron表达式(Quartz格式)。7段式：秒 分 时 日 月 周 年。",
        "cron表达式(7段式)。字段顺序：秒 分 时 日 月 周 年。星期编号0=周日…6=周六"
        "（与系统解析croniter一致，非Quartz的1=SUN），推荐字母缩写，"
        "如每周三、周五17:30 -> '0 30 17 ? * WED,FRI *'。",
    ),
    # cn：cron_expr 字段描述（大写 C 开头，legacy 扁平字段）
    (
        "Cron表达式(Quartz格式)。7段式：秒 分 时 日 月 周 年。",
        "Cron表达式(7段式)。字段顺序：秒 分 时 日 月 周 年。星期编号0=周日…6=周六"
        "（与系统解析croniter一致，非Quartz的1=SUN），推荐字母缩写，"
        "如每周三、周五17:30 -> '0 30 17 ? * WED,FRI *'。",
    ),
    # en：范围声明
    (
        "dow(1-7 or ?)",
        "dow(0-6 or ?; 0=SUN...6=SAT; "
        "prefer alpha abbreviations like WED/FRI to avoid ambiguity)",
    ),
    # en：整除规则句（工具级描述与 cron_create_job 描述共用）
    (
        "Dow(1-7): */X only works for X dividing 7: 1/7. 1=SUN, 7=SAT.",
        "Dow(0-6): */X only works for X dividing 7: 1/7. "
        "Numbering is 0=SUN...6=SAT (croniter convention, NOT Quartz 1=SUN); "
        "prefer alpha abbreviations like WED/FRI to avoid ambiguity.",
    ),
    # en：expr 字段描述（schedule.expr 与 legacy cron_expr 共用同一文本）
    (
        "Cron expression (Quartz format). 7-field: second minute hour day month dow year. ",
        "Cron expression (7-field). 7-field: second minute hour day month dow year. "
        "Dow numbering is 0=SUN...6=SAT (croniter convention, NOT Quartz 1=SUN); "
        "prefer alpha abbreviations, e.g. every Wed/Fri 17:30 -> '0 30 17 ? * WED,FRI *'. ",
    ),
)


def _apply_cron_dow_fix(text: str) -> str:
    """对单段文本应用 dow 语义替换（幂等）。"""
    fixed = str(text or "")
    for old, new in _CRON_DOW_SEMANTIC_FIXES:
        if old in fixed:
            fixed = fixed.replace(old, new)
    return fixed


def _patch_cron_dow_semantics(value: Any) -> Any:
    """递归修正 cron 工具描述中的 dow 编号语义（str 应用替换，dict/list 递归重建）。"""
    if isinstance(value, str):
        return _apply_cron_dow_fix(value)
    if isinstance(value, dict):
        return {key: _patch_cron_dow_semantics(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_patch_cron_dow_semantics(item) for item in value]
    return value


def _collect_string_blob(value: Any, *, out: list[str]) -> None:
    """收集 value 内所有字符串（用于残留告警检查）。"""
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_string_blob(item, out=out)
    elif isinstance(value, list):
        for item in value:
            _collect_string_blob(item, out=out)


# 旧的 Quartz dow 声明模式（1=SUN...7=SAT / 范围写成 1-7）。
# 用于残留检测：修正文本中的解释性"非 Quartz 的 1=SUN"不会误命中
# （它没有 ",7=SAT" 后缀，也不是 "(1-7" 范围声明）。
_QUARTZ_DOW_DECLARATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"1\s*[=＝]\s*SUN\s*[,，]\s*7\s*[=＝]\s*SAT"),
    re.compile(r"[Dd]ow\s*\(\s*1\s*-\s*7"),
    re.compile(r"周\s*\(\s*1\s*-\s*7"),
)


def _patch_cron_tool_cards(tools: list[Any]) -> list[Any]:
    """修正 cron 工具 ToolCard 的 description/input_params 中的 dow 语义，返回修正后的 tools。"""
    for tool in tools:
        card = getattr(tool, "card", None)
        if card is None:
            continue
        new_description = _apply_cron_dow_fix(str(getattr(card, "description", None) or ""))
        if new_description != getattr(card, "description", None):
            card.description = new_description
        if getattr(card, "input_params", None) is not None:
            card.input_params = _patch_cron_dow_semantics(card.input_params)
    # 残留告警：openjiuwen 文本若改版导致替换失配，残留的 Quartz dow 声明
    # 说明语义修正未完全生效
    for tool in tools:
        card = getattr(tool, "card", None)
        if card is None:
            continue
        blob: list[str] = []
        _collect_string_blob(getattr(card, "description", None) or "", out=blob)
        _collect_string_blob(getattr(card, "input_params", None) or {}, out=blob)
        if any(pattern.search(text) for text in blob for pattern in _QUARTZ_DOW_DECLARATION_PATTERNS):
            logger.warning(
                "[CronRuntimeBridge] openjiuwen cron 工具描述中的 dow 语义修正未完全生效，"
                "仍残留 Quartz 声明（1=SUN...7=SAT）：tool=%s",
                getattr(card, "name", None),
            )
    return tools


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
        tools = list(tools or [])
        # 修正 openjiuwen 工具描述中的 dow 编号语义（1=SUN→0=SUN，与 croniter 一致），
        # 见模块顶部 _CRON_DOW_SEMANTIC_FIXES 说明。
        tools = _patch_cron_tool_cards(tools)
        logger.info("[CronRuntimeBridge] Built %d cron tools: %s", 
                    len(tools), 
                    [tool.card.name if hasattr(tool, 'card') else str(tool) for tool in tools])
        return tools
