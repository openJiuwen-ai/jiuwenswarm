from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard
from jiuwenswarm.runtime.cron.cron_expr import normalize_cron_expr
from jiuwenswarm.runtime.cron.store import CronJobStore, _PROACTIVE_TICK_MODE
from jiuwenswarm.runtime.cron.models import (
    CronJob,
    CronTargetChannel,
    cron_job_modes_for_tools,
    is_valid_target_channel_id,
    normalize_cron_job_mode,
    normalize_target_channel_id,
    validate_cron_model,
)
from jiuwenswarm.common.utils import get_cron_jobs_path
from jiuwenswarm.runtime.host_services import send_runtime_push

logger = logging.getLogger(__name__)


def _cron_next_push_dt(cron_expr: str, base_dt: datetime) -> datetime:
    """Compute the next cron time without importing the Gateway scheduler."""
    from croniter import croniter  # type: ignore

    second_at_beginning = len(cron_expr.strip().split()) == 7
    next_dt = croniter(
        cron_expr,
        base_dt,
        second_at_beginning=second_at_beginning,
    ).get_next(datetime)
    if not isinstance(next_dt, datetime):
        raise RuntimeError("croniter returned invalid datetime")
    if next_dt.tzinfo is None:
        return next_dt.replace(tzinfo=base_dt.tzinfo)
    return next_dt


# AgentOS 下 job store 只属于 Gateway。该进程内快照由 Gateway 在每次用户
# Agent 请求前经 E2A 下发，仅用于 cron 工具的读取和后续 mutation 校验；绝不
# 写入 cron_jobs.json，因此 AgentServer 回收后不会形成第二份持久状态。
# Snapshots are scoped by the AgentOS routing user.  An AgentServer can serve
# several users concurrently, so a single process-wide "last snapshot" would
# let one request overwrite another user's cron view.
_gateway_jobs_snapshots: dict[str, dict[str, CronJob]] = {}


def install_gateway_jobs_snapshot(rows: list[Any], *, user_id: str = "") -> int:
    """Replace one routed user's in-memory Gateway-owned cron view."""
    snapshot: dict[str, CronJob] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            job = CronJob.from_dict(row)
        except Exception as exc:  # noqa: BLE001 - ignore one malformed stored row
            logger.warning("[CronTools] ignore malformed Gateway cron snapshot row: %s", exc)
            continue
        snapshot[job.id] = job
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        raise ValueError("user_id is required for Gateway cron snapshots")
    _gateway_jobs_snapshots[normalized_user_id] = snapshot
    return len(snapshot)


# ── run_now 反向确认 ──────────────────────────────────────────────────────────
# CronTools.run_now 经 gateway_push 异步提交 Gateway，Gateway 处理后把 run_id
# 经 E2A（cron.run_now.ack）回传，这里按发起请求的 request_id 关联等待方。
# 无 request_id（单用户 legacy）或回传超时/失败时，run_now 降级为不返回 run_id。
_gateway_run_acks: dict[str, asyncio.Future[str]] = {}
_gateway_command_acks: dict[str, asyncio.Future[dict[str, Any]]] = {}


def resolve_gateway_cron_command_ack(command_id: str, result: dict[str, Any]) -> None:
    future = _gateway_command_acks.pop(str(command_id or ""), None)
    if future is not None and not future.done():
        future.set_result(dict(result or {}))


_RUN_NOW_ACK_TIMEOUT_SEC = 10.0

# ── pending scope 回收 ────────────────────────────────────────────────────────
# pending 投影的生命周期是「同一 Agent turn 内、Gateway 落库前」；TTL 宽裕到
# 分钟级以覆盖慢创建（如大模型校验），硬上限防御 request_id 病理抖动。
_PENDING_SCOPE_TTL_SEC = 30 * 60.0
_PENDING_SCOPE_MAX = 128


def register_gateway_run_ack(request_id: str) -> asyncio.Future[str] | None:
    """注册等待 Gateway run_now 确认的 Future（按 request_id 关联）。"""
    normalized = str(request_id or "").strip()
    if not normalized:
        return None
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()
    _gateway_run_acks[normalized] = fut
    return fut


def resolve_gateway_run_ack(request_id: str, run_id: str) -> None:
    """AgentServer 收到 Gateway 的 cron.run_now.ack 时唤醒对应等待方。"""
    normalized = str(request_id or "").strip()
    if not normalized:
        return
    fut = _gateway_run_acks.pop(normalized, None)
    if fut is None or fut.done():
        return
    fut.set_result(str(run_id or ""))

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
    model_name: str = ""  # 当前会话模型；创建任务时缺省继承，保证定时执行一致
    user_id: str = ""  # 发起会话的用户路由键；AgentOS 下随 E2A 请求透传，供 job 归属创建者


class CronTools:
    """Agent-side cron tools：job 变更经 E2A 转发 Gateway 单源落库（Phase 4）。

    路由用 ContextVar 按 Task 隔离（与 interface 中 ``push_cron_route`` / ``reset_cron_route`` 配对）；
    同进程一套 LocalFunction，并发安全依赖当前 asyncio 任务的上下文而非单例可变字段。
    收敛约束（方案 §4 / §10.9）：
      - 不在 AgentServer 本地持久化 job、不写用户目录 ``cron_jobs.json``；
      - 不启动第二个调度器（job store / 调度 / 触发 / 生命周期统一由 Gateway 持有）；
      - agent 发起的 job 创建/更新/取消/启停/立即执行一律经 E2A 转发 Gateway 落库。
    本地 ``_local_store`` 仅作已落库任务的只读视图（单用户下与 Gateway
    共享同一文件）。本进程另维护未确认 mutation 的内存投影，以保证一次工具
    调用中 create → list/update/preview 具有一致视图，但绝不写用户目录。

    数据模型、校验和持久化实现位于 Runtime；常驻调度生命周期仍由 Gateway
    单一持有。一次一进程的 CLI 没有 resident host，不会启动后台调度服务。
    """

    def __init__(
        self,
        gateway_push: Any | None = None,
        *,
        agent_client: Any | None = None,
        message_handler: Any | None = None,
    ) -> None:
        self._gateway_push = gateway_push
        # 只读视图：不落库；create/update/delete/toggle 均经 E2A 转发 Gateway 单源。
        self._local_store = CronJobStore(
            path=get_cron_jobs_path()
        )
        # A pending projection only exists to make multiple tool calls in the
        # *same Agent turn* coherent before Gateway has persisted the change.
        # It must not cross request or user boundaries.
        self._pending_views: dict[str, dict[str, CronJob]] = {}
        self._pending_deletes_by_request: dict[str, set[str]] = {}
        # scope → 最近使用时间（monotonic）：backend/CronTools 为进程级单例，
        # 无 turn 结束钩子，用惰性 TTL + 上限驱逐回收 pending scope，防止
        # scope（request:<id> 等）随请求无限累积。
        self._pending_scope_last_used: dict[str, float] = {}
        self._scheduler: None = None
        self._agent_client = agent_client
        self._message_handler = message_handler
        self._scheduler_started = False

    async def ensure_scheduler(self) -> None:
        """AgentServer 不再持有调度器（Phase 4 单源收敛），恒返回 ``None``。

        保留方法签名兼容调用方（如 ``CronRuntimeBridge.ensure_scheduler_started``），
        但不再创建/启动第二个 ``CronSchedulerService``——cron 的调度与触发统一由
        Gateway 长期进程负责，AgentServer 启动第二个调度器会导致双调度器重复触发。
        """
        self._scheduler_started = True
        return None

    async def _reload_scheduler(self) -> None:
        """AgentServer 无本地调度器，无需 reload（Phase 4 单源收敛）。"""
        return None

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

    def _pending_scope(self) -> str:
        """Return an isolated transient-view key for the current tool turn."""
        route = self._route()
        request_id = str(route.request_id or "").strip()
        if request_id:
            return f"request:{request_id}"
        # The fallback retains the legacy shared-directory behaviour for
        # callers that predate request IDs, while still separating AgentOS
        # users whenever a routing user is available.
        user_id = str(route.user_id or "").strip()
        return f"user:{user_id}" if user_id else "legacy"

    def _pending_view_for_route(self) -> dict[str, CronJob]:
        scope = self._pending_scope()
        self._touch_pending_scope(scope)
        return self._pending_views.setdefault(scope, {})

    def _pending_deletes_for_route(self) -> set[str]:
        scope = self._pending_scope()
        self._touch_pending_scope(scope)
        return self._pending_deletes_by_request.setdefault(scope, set())

    def _touch_pending_scope(self, scope: str) -> None:
        """记录 scope 活跃时间并惰性回收过期/超限的 pending scope。

        pending 投影只需覆盖「同一 Agent turn、Gateway 落库前」的短暂窗口
        （正常毫秒级完成）；TTL 取宽裕上限，过期后由共享 store / Gateway
        快照提供权威视图。回收同时清掉 Gateway 落库失败遗留的幽灵投影。
        scope 总量有硬上限，扫描成本 O(n) 可忽略。
        """
        now = time.monotonic()
        last_used = self._pending_scope_last_used
        for s in [
            s for s, ts in last_used.items()
            if now - ts > _PENDING_SCOPE_TTL_SEC
        ]:
            self._pending_views.pop(s, None)
            self._pending_deletes_by_request.pop(s, None)
            last_used.pop(s, None)
        last_used[scope] = now
        if len(last_used) > _PENDING_SCOPE_MAX:
            # 超限时按最久未使用驱逐，保住当前 scope
            for s in sorted(last_used, key=last_used.get):
                if len(last_used) <= _PENDING_SCOPE_MAX:
                    break
                if s == scope:
                    continue
                self._pending_views.pop(s, None)
                self._pending_deletes_by_request.pop(s, None)
                last_used.pop(s, None)

    def _snapshot_for_route(self) -> dict[str, CronJob] | None:
        user_id = str(self._route().user_id or "").strip()
        if not user_id:
            return None
        return _gateway_jobs_snapshots.get(user_id)

    def _uses_gateway_command_ack(self) -> bool:
        """Whether the selected host transport supports command acknowledgements.

        Production AgentServer uses the host-services path (``gateway_push is
        None``), whose Gateway round trip delivers ``CRON_COMMAND_ACK``.  A
        custom transport remains fire-and-forget unless it explicitly opts in;
        this keeps test/legacy transports compatible without importing Server
        or WebSocket implementations into the Runtime-owned cron tools.
        """
        if self._gateway_push is None:
            return True
        return bool(getattr(self._gateway_push, "supports_cron_command_ack", False))

    async def _send_split(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        from jiuwenswarm.common.e2a.constants import E2A_RESPONSE_KIND_CRON

        r = self._route()
        # ``send_push`` is delivered on a side channel.  The originating chat
        # stream can finish and be removed from Gateway's in-memory request map
        # before its side-channel frame is scheduled.  Keep the authenticated
        # routing owner on the internal frame so that a late cron mutation does
        # not get persisted with an empty ``user_id`` and disappear from the
        # Web user's task list.  This is derived solely from the runtime route,
        # never from an LLM-supplied tool parameter.
        push_metadata: dict[str, Any] = {}
        route_user_id = str(r.user_id or "").strip()
        if route_user_id:
            push_metadata["_jiuwenswarm_cron_owner_user_id"] = route_user_id
        command_id = f"cron-{uuid.uuid4().hex}"
        payload = {
            "request_id": r.request_id,
            "channel_id": r.channel_id,
            "session_id": r.session_id,
            "metadata": push_metadata,
            "response_kind": E2A_RESPONSE_KIND_CRON,
            "body": {
                "command_id": command_id,
                "action": action,
                "status": "ok",
                "data": dict(params or {}),
                "message": "",
            },
        }
        # Wait for the Gateway-owned controller's result rather than merely
        # confirming socket acceptance.  This surfaces Gateway-side validation
        # errors (invalid cron_expr, project binding failures, etc.) back to
        # the agent in both AgentOS and legacy single-user modes.
        ack: asyncio.Future[dict[str, Any]] | None = None
        if self._uses_gateway_command_ack():
            ack = asyncio.get_running_loop().create_future()
            _gateway_command_acks[command_id] = ack
        try:
            if self._gateway_push is not None:
                delivered = await self._gateway_push.send_push(payload)
            else:
                delivered = await send_runtime_push(payload)
        except BaseException:
            _gateway_command_acks.pop(command_id, None)
            raise
        # 传输层明确返回 False 代表 Gateway 不可达/写入失败。不能再把这种情况
        # 伪装成已转发，否则 create/update 只停留在本进程 pending view、任务永不落库。
        # 为兼容旧的自定义 transport，None 仍视为未知但已接受；内置 WS transport
        # 必定返回 bool。
        if delivered is False:
            _gateway_command_acks.pop(command_id, None)
            raise RuntimeError("cron request could not be delivered to gateway")
        if ack is not None:
            try:
                result = await asyncio.wait_for(asyncio.shield(ack), timeout=15)
            except asyncio.TimeoutError as exc:
                _gateway_command_acks.pop(command_id, None)
                raise RuntimeError("gateway cron command acknowledgement timed out") from exc
            data = result.get("data")
            if result.get("error"):
                raise RuntimeError(str(result["error"]))
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(str(data["error"]))
            return {"action": action, "status": "ok", "data": data}
        # This transport acknowledgement only confirms that Gateway accepted
        # the frame.  It is deliberately not reported as a successful cron
        # mutation: persistence validation happens asynchronously in Gateway.
        return {
            "action": action,
            "status": "submitted",
            "data": None,
            "message": "cron request submitted to gateway; awaiting gateway confirmation",
        }

    async def _send(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._send_split(action, params)

    async def _view_jobs(self) -> list[CronJob]:
        """Return Gateway-store snapshot overlaid by this request process's pending view."""
        # AgentOS AgentServer has no cron_jobs.json by design.  Its snapshot is
        # delivered by Gateway immediately before the user request starts.  The
        # legacy shared-directory layout receives no snapshot and retains the
        # historical direct read of Gateway's shared store.
        snapshot = self._snapshot_for_route()
        if snapshot is None:
            jobs = {job.id: job for job in await self._local_store.list_jobs()}
        else:
            jobs = dict(snapshot)
        jobs.update(self._pending_view_for_route())
        for job_id in self._pending_deletes_for_route():
            jobs.pop(job_id, None)
        return sorted(
            jobs.values(),
            key=lambda job: (job.updated_at or 0.0, job.created_at or 0.0),
            reverse=True,
        )

    async def _view_job(self, job_id: str) -> CronJob | None:
        normalized_id = str(job_id or "").strip()
        pending_deletes = self._pending_deletes_for_route()
        if not normalized_id or normalized_id in pending_deletes:
            return None
        pending = self._pending_view_for_route().get(normalized_id)
        if pending is not None:
            return pending
        snapshot = self._snapshot_for_route()
        if snapshot is not None:
            return snapshot.get(normalized_id)
        return await self._local_store.get_job(normalized_id)

    @staticmethod
    def _merge_pending_job(existing: CronJob, patch: dict[str, Any]) -> CronJob:
        data = existing.to_dict()
        for key, value in patch.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        return CronJob.from_dict(data)

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
        if self._uses_gateway_command_ack():
            result = await self._send("list", {})
            return list(result.get("data") or [])
        jobs = await self._view_jobs()
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
        if self._uses_gateway_command_ack():
            return (await self._send("get", {"job_id": job_id})).get("data")
        job = await self._view_job(job_id)
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
        if "model_name" not in normalized:
            inherited_model = str(r.model_name or "").strip()
            if inherited_model:
                normalized["model_name"] = inherited_model
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

        # Phase 4 单源收敛：AgentServer 不本地持久化 job，仅经 E2A 转发 Gateway 落库。
        # 用 build_job（不落盘）拿到规范化视图（含 round-trip 校验）供返回与转发。
        # user_id：优先工具参数，其次当前请求路由上下文（AgentOS 随 E2A 请求透传），
        # 保证 agent 创建的任务归属发起会话的用户，web 端按 user_id 隔离时可见。
        job = self._local_store.build_job(
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
            user_id=str(normalized.get("user_id") or "").strip() or r.user_id,
            **session_kw,
            **mode_kw,
            **model_kw,
        )
        sync_payload = job.to_dict()
        sync_payload["project_dir"] = project_dir_val
        await self._send("create", sync_payload)
        self._pending_deletes_for_route().discard(job.id)
        self._pending_view_for_route()[job.id] = job
        result = job.to_dict()
        result["gateway_mutation_status"] = "submitted"
        return result

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
        existing = await self._view_job(job_id)
        remote_gateway = self._uses_gateway_command_ack()
        if existing is None:
            if not remote_gateway:
                raise KeyError("job not found")

        channel_id_val = self._resolve_channel_id() or "web"
        from jiuwenswarm.server.runtime.session.project_store import resolve_cron_job_patch
        if existing is not None:
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

        # Phase 4 单源收敛：不本地持久化，仅经 E2A 转发 Gateway 落库。
        # 返回值 = existing 视图 + patch（None 值表示清除该字段）。
        gateway_result = await self._send(
            "update",
            {"job_id": job_id, "patch": self._sync_patch_payload(normalized_patch)},
        )
        if remote_gateway:
            return gateway_result.get("data")
        updated = self._merge_pending_job(existing, normalized_patch)
        self._pending_view_for_route()[updated.id] = updated
        result = updated.to_dict()
        result["gateway_mutation_status"] = "submitted"
        return result

    async def delete_job(self, job_id: str) -> Any:
        # proactive.tick 保护：读只读视图拦截（与 toggle_job 一致），
        # 避免把受保护的定时任务删除请求转发给 Gateway 后再报错。
        existing = await self._view_job(job_id)
        remote_gateway = self._uses_gateway_command_ack()
        if existing is not None and str(getattr(existing, "mode", "") or "").strip().lower() == "proactive.tick":
            raise RuntimeError(
                "主动推荐定时任务由设置→主动推荐开关控制，不能删除；请到设置关闭开关。"
            )
        if existing is None:
            if remote_gateway:
                result = await self._send("delete", {"job_id": job_id})
                return bool((result.get("data") or {}).get("deleted"))
            # Gateway's AgentOS snapshot is best-effort.  A restarted
            # AgentServer (or a failed pre-turn sync) must still be able to ask
            # the Gateway-owned store to delete a real job; Gateway performs
            # the authoritative existence and ownership checks.
            if self._agentos_snapshot_unavailable():
                await self._send("delete", {"job_id": job_id})
                self._pending_deletes_for_route().add(job_id)
                return True
            # 与迁移前 store.delete_job 契约一致：job 不存在时不提交 Gateway，
            # 返回 False 让调用方可感知（避免 LLM 幻觉 job_id 得到假成功）。
            return False
        await self._send("delete", {"job_id": job_id})
        self._pending_view_for_route().pop(job_id, None)
        self._pending_deletes_for_route().add(job_id)
        # 上游 CronToolBackend / wrapper 契约声明 delete 返回 bool；True 表示
        # 删除请求已提交 Gateway 单源（异步落库），不得返回 dict 破坏单用户兼容。
        return True

    async def toggle_job(self, job_id: str, enabled: bool) -> Any:
        # proactive.tick job 的开关由 config 的 proactive_recommendation.enabled 驱动，
        # 禁止手动 toggle——否则会与 config 开关不一致。引导用户去设置关开关。
        existing = await self._view_job(job_id)
        remote_gateway = self._uses_gateway_command_ack()
        if existing is not None and str(getattr(existing, "mode", "") or "").strip().lower() == "proactive.tick":
            raise RuntimeError(
                "主动推荐定时任务由设置→主动推荐开关控制，不能手动启停；请到设置→主动推荐操作。"
            )
        if existing is None:
            if remote_gateway:
                return (await self._send("toggle", {"job_id": job_id, "enabled": bool(enabled)})).get("data")
            # See delete_job: do not turn a missing best-effort AgentOS snapshot
            # into a false "job not found" result.
            if self._agentos_snapshot_unavailable():
                await self._send("toggle", {"job_id": job_id, "enabled": bool(enabled)})
                return {
                    "id": job_id,
                    "enabled": bool(enabled),
                    "gateway_mutation_status": "submitted",
                }
            # 与迁移前 store.toggle_job 契约一致：job 不存在抛 KeyError。
            raise KeyError("job not found")
        await self._send("toggle", {"job_id": job_id, "enabled": bool(enabled)})
        updated = self._merge_pending_job(existing, {"enabled": bool(enabled)})
        self._pending_view_for_route()[updated.id] = updated
        result = updated.to_dict()
        result["gateway_mutation_status"] = "submitted"
        return result

    def _agentos_snapshot_unavailable(self) -> bool:
        """Whether this routed AgentOS request lacks Gateway's ephemeral view."""
        return bool(str(self._route().user_id or "").strip()) and self._snapshot_for_route() is None

    async def preview_job(self, job_id: str, count: int = 5) -> Any:
        if self._uses_gateway_command_ack():
            return (await self._send("preview", {"job_id": job_id, "count": count})).get("data")
        job = await self._view_job(job_id)
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
        """提交立即执行并等待 Gateway 回传 run_id（P2 修复）。

        Gateway 异步处理后经 E2A（cron.run_now.ack）按 request_id 回传 run_id。
        无 request_id（单用户 legacy）或超时/回传失败时降级返回 submitted 状态，
        不阻塞 agent 主流程。
        """
        route = self._route()
        request_id = str(route.request_id or "").strip()
        ack_fut = register_gateway_run_ack(request_id) if request_id else None
        try:
            await self._send("run_now", {"job_id": job_id})
        except asyncio.CancelledError:
            if ack_fut is not None:
                _gateway_run_acks.pop(request_id, None)
                if not ack_fut.done():
                    ack_fut.cancel()
            raise
        except Exception:
            if ack_fut is not None:
                _gateway_run_acks.pop(request_id, None)
                if not ack_fut.done():
                    ack_fut.cancel()
            raise
        if ack_fut is None:
            return {
                "action": "run_now",
                "status": "submitted",
                "data": None,
                "message": "cron run_now submitted to gateway",
            }
        try:
            run_id = await asyncio.wait_for(
                asyncio.shield(ack_fut), timeout=_RUN_NOW_ACK_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            # shield 保护了 ack_fut 不被 wait_for 取消；超时后主动清理，
            # 避免 Future 长期滞留 _gateway_run_acks（后续 resolve 到达时
            # fut.done() 已为 True，set_result 被忽略，无副作用）。
            _gateway_run_acks.pop(request_id, None)
            logger.warning(
                "[CronTools] run_now ack timeout for request=%s job=%s", request_id, job_id
            )
            run_id = ""
        except asyncio.CancelledError:
            _gateway_run_acks.pop(request_id, None)
            raise
        if not run_id:
            return {
                "action": "run_now",
                "status": "submitted",
                "data": None,
                "message": "cron run_now submitted to gateway (no run_id ack)",
            }
        return {"action": "run_now", "status": "ok", "data": {"run_id": run_id}}

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
