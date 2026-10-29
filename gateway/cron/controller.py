from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, ClassVar

from zoneinfo import ZoneInfo

from gateway.common.scheduled_task_policy import is_scheduled_task_supported
from gateway.common.work_mode import (
    DEFAULT_PROJECT_ID_CODE,
    DEFAULT_PROJECT_ID_WORK,
    EXECUTION_WORK_MODES,
)
from gateway.enterprise import is_enterprise
from gateway.cron.cron_expr import (
    clamp_wake_offset_for_delay_seconds,
    normalize_cron_expr,
)
from gateway.cron.models import (
    CRON_JOB_DESCRIPTION_MAX_LENGTH,
    CRON_JOB_NAME_MAX_LENGTH,
    CRON_MODEL_SOURCE_LOGIN,
    CronTargetChannel,
    cron_job_metadata,
    is_valid_target_channel_id,
    normalize_cron_job_mcp,
    normalize_cron_job_mode,
    normalize_tenant_id,
    normalize_target_channel_id,
    resolve_cron_model,
)
from gateway.cron.proactive_cron_sync import PROACTIVE_JOB_ID
from gateway.cron.scheduler import CronSchedulerService, _cron_next_push_dt
from gateway.cron.store_base import CronJobStoreBackend
from gateway.runtime.cron.cron_job_mutations import strip_sticky_identity_fields

logger = logging.getLogger(__name__)

# 列表/调度等批量路径查询"项目准入"闸门时的最大并发数。
_GATE_QUERY_CONCURRENCY = 8


def _login_credential_ref_from_session(auth_session: str) -> str:
    """从服务端登录会话取华为账号身份，派生登录免费模型的凭据句柄绑定。

    项目自身的路由 user_id（Web URL/localStorage）与华为账号登录是两套独立
    用户体系，登录凭据按华为账号 openid 登记、两者没有映射——绑定只能来自
    创建请求连接上的登录会话（``_auth_session``，由 web handler 注入）。
    没有可用会话时明确拒绝，不留一个到点必失败的任务。
    """
    from gateway.auth.login_credentials import credential_ref_for_user
    from gateway.auth.service import ModelAuthRequired, live_session

    try:
        session = live_session(auth_session or None, allow_refresh=False)
    except ModelAuthRequired as exc:
        raise ValueError(
            "该模型需要登录华为账号后使用（未登录或登录已过期），请登录后重试"
        ) from exc
    return credential_ref_for_user(session.user_id)


def _required_enterprise_tenant_id(raw: Any) -> str:
    """企业版创建时三个身份必须都有内容，空白不补成 default。"""
    if isinstance(raw, str):
        text = raw.strip()
        if text:
            return text
    raise ValueError("enterprise cron requires group_id, bot_id and user_id")


def _serialize_mutation(method):
    """Keep project fencing/cleanup and cron creation in one admission order."""

    @wraps(method)
    async def serialized(self, *args, **kwargs):
        async with self.mutation_lock:
            return await method(self, *args, **kwargs)

    return serialized


class CronController:
    """High-level cron API used by WebChannel handlers. Singleton."""

    _instance: ClassVar[CronController | None] = None

    def __init__(self, *, store: CronJobStoreBackend, scheduler: CronSchedulerService) -> None:
        self._store = store
        self._scheduler = scheduler
        if not hasattr(scheduler, "_lifecycle_mutation_lock"):
            scheduler._lifecycle_mutation_lock = asyncio.Lock()
        self.mutation_lock = scheduler._lifecycle_mutation_lock
        self._target_channel: CronTargetChannel | None = None
        self._agent_client: Any = None
        # 准入闸门是跨进程 RPC,列表时按项目去重后并发查询,这里限制并发上限,
        # 避免任务/项目很多时一次性打出上百个请求。
        self._gate_concurrency = asyncio.Semaphore(_GATE_QUERY_CONCURRENCY)

    async def _gate_allowed(
        self,
        project_id: str | None,
        user_id: str | None,
        group_id: str | None = None,
        bot_id: str | None = None,
    ) -> bool:
        async with self._gate_concurrency:
            return await self._scheduler.project_execution_allowed(
                project_id, user_id, group_id, bot_id
            )

    @property
    def store(self) -> CronJobStore:
        """The underlying cron job store."""
        return self._store

    @property
    def scheduler(self) -> CronSchedulerService:
        """The underlying cron scheduler service."""
        return self._scheduler

    def set_target_channel(self, channel: CronTargetChannel) -> None:
        self._target_channel = channel

    def bind_agent_client(self, agent_client: Any) -> None:
        """Keep the Gateway client used to read AgentServer's Zen cache."""
        self._agent_client = agent_client

    async def _zen_entries(self) -> list[dict[str, Any]]:
        client = self._agent_client
        if client is None:
            return []
        from gateway.zen_query import fetch_zen_free_model_rows

        return await fetch_zen_free_model_rows(client)

    @classmethod
    def get_instance(
        cls,
        *,
        store: CronJobStoreBackend | None = None,
        scheduler: CronSchedulerService | None = None,
    ) -> CronController:
        """Return the singleton instance.

        On first call, store and scheduler are required to create the instance.
        On subsequent calls, both can be omitted to get the existing instance.

        Args:
            store: Required only on first call.
            scheduler: Required only on first call.

        Returns:
            The singleton CronController.

        Raises:
            RuntimeError: If instance not yet initialized and store/scheduler not provided.
        """
        if cls._instance is not None:
            return cls._instance
        if store is None or scheduler is None:
            raise RuntimeError(
                "CronController not initialized. Call get_instance(store=..., scheduler=...) first."
            )
        cls._instance = cls(store=store, scheduler=scheduler)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton. For testing only."""
        cls._instance = None

    @staticmethod
    def _validate_schedule(*, cron_expr: str, timezone: str) -> None:
        tz = ZoneInfo(timezone)
        base = datetime.now(tz=tz)
        _ = _cron_next_push_dt(cron_expr, base)

    @staticmethod
    def _clamp_wake_offset_for_upcoming_run(
        wake_offset_seconds: Any,
        *,
        cron_expr: str,
        timezone: str,
    ) -> int:
        """企业版创建：提前量不能超过离下次触发还剩的秒数。"""
        try:
            tz = ZoneInfo(timezone)
            now = datetime.now(tz=tz)
            push_dt = _cron_next_push_dt(cron_expr, now)
            delay = float(push_dt.timestamp() - now.timestamp())
            clamped = clamp_wake_offset_for_delay_seconds(wake_offset_seconds, delay)
            if wake_offset_seconds is not None:
                try:
                    before: Any = int(wake_offset_seconds)
                except (TypeError, ValueError):
                    before = wake_offset_seconds
            else:
                before = None
            if before != clamped:
                logger.info(
                    "[Cron] clamp wake_offset for upcoming run delay≈%.1fs: %s -> %s",
                    delay,
                    before,
                    clamped,
                )
            return clamped
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Cron] skip wake_offset clamp: %s", exc)
            if wake_offset_seconds is None:
                return 0
            try:
                return max(0, int(wake_offset_seconds))
            except (TypeError, ValueError):
                return 0

    _DESCRIPTION_TIME_KEYWORDS = (
        "每天",
        "每周",
        "每月",
        "上午",
        "下午",
        "早上",
        "晚上",
        "凌晨",
    )

    def _normalize_targets(self, raw: Any) -> str:
        """将 targets 规范为 CronTargetChannel 枚举值。"""
        raw_s = str(raw or "").strip()
        if self._target_channel is None and not raw_s:
            raise ValueError("targets is required when target_channel is not set")
        if not raw_s:
            target = normalize_target_channel_id(self._target_channel.value)
        else:
            if not is_valid_target_channel_id(raw_s):
                raise ValueError(
                    "targets must be one of tui/web/feishu/dingtalk/whatsapp/wecom/wechat"
                    " or feishu_enterprise:<app_id>"
                )
            target = normalize_target_channel_id(raw_s)
        if not is_scheduled_task_supported(target):
            raise ValueError(f"targets does not support scheduled tasks: {target}")
        return target

    @classmethod
    def _normalize_description(cls, description: str, name: str) -> str:
        """若 description 含时间/频率用语且 name 为纯任务，则只保留任务内容（用 name）。"""
        description = (description or "").strip()
        name = (name or "").strip()
        if not name:
            return description
        if not any(kw in description for kw in cls._DESCRIPTION_TIME_KEYWORDS):
            return description
        if name in description or description.endswith(name):
            return name
        return description

    @staticmethod
    def _routing_session_id(targets: str, raw: Any) -> str | None:
        """Accept session_id for all channels; feishu_enterprise requires SessionMap format."""
        targets_s = str(targets or "").strip()
        raw_s = str(raw or "").strip() if isinstance(raw, str) else ""
        if not raw_s:
            return None
        if targets_s.startswith("feishu_enterprise:"):
            if "::" not in raw_s:
                return None
            parts = raw_s.split("::")
            if len(parts) < 3 or parts[0] != "feishu":
                return None
            return raw_s
        return raw_s

    @staticmethod
    def _matches_list_filter(
        job: Any,
        *,
        project_id: str,
        user_id: str,
        group_id: str,
        bot_id: str,
    ) -> bool:
        """用户、群、机器人都要比。项目编号可以不比。

        用户、群、机器人空着等于 ``default``。``project_id is None`` 表示不按项目筛，
        定时任务总表和侧边栏要看到每个项目下的任务。空字符串才是默认项目，
        不收成 ``default``。
        """
        if project_id is not None:
            current_project = str(getattr(job, "project_id", "") or "")
            if current_project != str(project_id or ""):
                return False
        return CronController._matches_tenant(
            job, user_id=user_id, group_id=group_id, bot_id=bot_id
        )

    @staticmethod
    def _matches_tenant(job: Any, *, user_id: str, group_id: str, bot_id: str) -> bool:
        """用户、群、机器人都要对上。空着等于 ``default``。"""
        if normalize_tenant_id(getattr(job, "user_id", "")) != normalize_tenant_id(user_id):
            return False
        if normalize_tenant_id(getattr(job, "group_id", "")) != normalize_tenant_id(group_id):
            return False
        if normalize_tenant_id(getattr(job, "bot_id", "")) != normalize_tenant_id(bot_id):
            return False
        return True

    @staticmethod
    def _owns_job(job: Any, *, user_id: str, group_id: str, bot_id: str) -> bool:
        """改、删、预览前核对三个身份。

        个人版没填的收成 ``default`` 再比。企业版缺一个就当不是这条任务，不补 ``default``。
        """
        if is_enterprise():
            try:
                wanted_group = _required_enterprise_tenant_id(group_id)
                wanted_bot = _required_enterprise_tenant_id(bot_id)
                wanted_user = _required_enterprise_tenant_id(user_id)
            except ValueError:
                return False
            if str(getattr(job, "group_id", "") or "").strip() != wanted_group:
                return False
            if str(getattr(job, "bot_id", "") or "").strip() != wanted_bot:
                return False
            if str(getattr(job, "user_id", "") or "").strip() != wanted_user:
                return False
            return True
        return CronController._matches_tenant(
            job, user_id=user_id, group_id=group_id, bot_id=bot_id
        )

    async def list_jobs(
        self,
        *,
        project_id: str | None = None,
        user_id: str = "",
        group_id: str = "",
        bot_id: str = "",
    ) -> list[dict[str, Any]]:
        """按用户、群、机器人筛选。项目编号可以再收窄。

        用户、群、机器人不传就是 ``default``。``project_id`` 不传表示这个人的全部项目。
        传空字符串才只要默认项目。筛完后再问项目准入：项目已被隐藏或暂时不能执行的任务不出现在列表里。
        同一个项目、同一个用户只问一次。
        """
        jobs = []
        for job in await self._store.list_jobs():
            if self._matches_list_filter(
                job,
                project_id=project_id,
                user_id=user_id,
                group_id=group_id,
                bot_id=bot_id,
            ):
                jobs.append(job)
        # project_execution_allowed 是一次跨进程询问。同项目同用户共享一次判定，
        # 去重后并发查询，避免一次列表变成逐任务串行往返。
        keys = list(dict.fromkeys(
            (job.project_id, job.user_id, job.group_id, job.bot_id) for job in jobs
        ))
        if not keys:
            return []
        verdicts = await asyncio.gather(
            *(self._gate_allowed(pid, uid, group_id, bot_id) for pid, uid, group_id, bot_id in keys)
        )
        allowed = dict(zip(keys, verdicts))
        return [
            job.to_dict()
            for job in jobs
            if allowed[(job.project_id, job.user_id, job.group_id, job.bot_id)]
        ]

    @_serialize_mutation
    async def hide_project_jobs(self, project_id: str, *, commit=None) -> dict:
        """项目移除(隐藏)时停止其下全部定时任务:停用 + 取消在途执行。

        不按 user_id 过滤:项目不是用户私有资源,「项目隐藏 ⇒ 其下任务全部
        停用」是不变量。若按操作者过滤,其他属主的任务会保持 enabled,项目
        恢复后直接回到触发状态,违背「恢复后默认停止」。任务记录原样保留,
        恢复项目后默认保持停用,由用户手动重新启用。

        返回的 stopped_cron_jobs 是项目下任务总数(含移除前已停用的),
        并非本次实际停用的数量。
        """
        # Keep admission closed until the AgentServer commits hidden=True.
        self._scheduler.close_project_admission(project_id)
        enabled_job_ids: list[str] = []
        try:
            jobs = [job for job in await self._store.list_jobs() if job.project_id == project_id]
            enabled_job_ids = [job.id for job in jobs if job.enabled]
            disable = getattr(self._store, "disable_project_jobs", None)
            if callable(disable):
                await disable(project_id)
            else:
                for job in jobs:
                    if job.enabled:
                        await self._store.update_job(job.id, {"enabled": False})
            await self._scheduler.reload()
            await self._scheduler.stop_project_runs(project_id)
            if commit is not None:
                await commit()
            return {"stopped_cron_jobs": len(jobs)}
        except Exception:
            # A rejected AgentServer commit must not leave visible-project jobs
            # disabled. Restore only jobs that were enabled before this attempt.
            try:
                for job_id in enabled_job_ids:
                    await self._store.update_job(job_id, {"enabled": True})
                if enabled_job_ids:
                    await self._scheduler.reload()
            except Exception:
                logging.getLogger(__name__).exception(
                    "failed to restore cron jobs after project remove failure: %s",
                    project_id,
                )
                raise
            raise
        finally:
            self._scheduler.reopen_project_admission(project_id)

    async def get_job(
        self,
        job_id: str,
        *,
        project_id: str | None = "",
        user_id: str = "",
        group_id: str = "",
        bot_id: str = "",
    ) -> dict[str, Any] | None:
        """按编号取出一条任务，再用和列表相同的条件核对。

        用户、群、机器人不传就是 ``default``。项目编号不传（这个函数的默认值）或传空字符串，
        都表示只要默认项目。传 ``None`` 表示不按项目筛，网页上的立即执行、编辑、删除走这条。
        对不上就当没有这条任务。这里不查项目现在能不能跑。
        """
        job = await self._store.get_job(job_id)
        if job is None:
            return None
        if not self._matches_list_filter(
            job,
            project_id=project_id,
            user_id=user_id,
            group_id=group_id,
            bot_id=bot_id,
        ):
            return None
        return job.to_dict()

    @staticmethod
    def job_metadata() -> dict[str, Any]:
        return cron_job_metadata()

    @_serialize_mutation
    async def create_job(
        self,
        params: dict[str, Any],
        *,
        internal_proactive_sync: bool = False,
    ) -> dict[str, Any]:
        # ``internal_proactive_sync`` is a Python-level keyword set only by
        # proactive_cron_sync. It deliberately does not live in ``params``:
        # every key in ``params`` can be supplied by a web/tui client (#5853).
        # This marker is set only by the AgentServer-to-Gateway path after the
        # project has been resolved against the user's AgentServer directory.
        # Do not persist it with the job payload.
        params = dict(params or {})
        allow_unresolved_project_id = bool(
            params.pop("_agentos_project_binding_verified", False)
        )
        # 登录会话 id：web handler 从创建连接上注入的服务端专有键（客户端传的
        # 会被无条件覆盖），仅用于登录免费模型的凭据绑定捕获，不落任务数据。
        auth_session = str(params.pop("_auth_session", "") or "").strip()
        enterprise = is_enterprise()
        name = str(params.get("name") or "").strip()
        cron_expr = normalize_cron_expr(str(params.get("cron_expr") or "").strip())
        timezone = (
            str(params.get("timezone") or "Asia/Shanghai").strip() or "Asia/Shanghai"
        )
        enabled = bool(params.get("enabled", True))
        description = str(params.get("description") or "")
        wake_offset_seconds = params.get("wake_offset_seconds", None)
        raw_targets = params.get("targets")
        mode = params.get("mode")
        if mode is not None and str(mode).strip():
            mode = normalize_cron_job_mode(mode)
        else:
            mode = None
        # proactive.tick 是主动推荐开关自动维护的内部模式（proactive_cron_sync
        # 以固定 id 注册）：用户路径创建同模式任务会被后端 mode 保护拒绝启停/
        # 删除，而前端只按固定 id 识别系统任务，导致任务永远停不掉（#5128）。
        # 豁免不能依赖客户端可控的 params.id：用户路径携带固定 id 会整体顶替
        # 内置任务（#5853），因此只认内部 sync 传入的关键字参数。
        requested_id = str(params.get("id") or "").strip()
        if not internal_proactive_sync:
            if mode == "proactive.tick":
                raise ValueError(
                    "proactive.tick 是系统内部模式，由设置→主动推荐开关自动维护，不能手动创建"
                )
            if requested_id == PROACTIVE_JOB_ID:
                raise ValueError(
                    f"{PROACTIVE_JOB_ID} 是系统内置主动推荐任务的保留 id，不能手动创建"
                )
        if enterprise:
            from gateway.cron.enterprise_model import (
                resolve_enterprise_cron_model,
            )

            group_id = _required_enterprise_tenant_id(params.get("group_id"))
            bot_id = _required_enterprise_tenant_id(params.get("bot_id"))
            user_id = _required_enterprise_tenant_id(params.get("user_id"))
            # 企业版核对当前身份在企业库里的对话模型，不读本机 config 占位名。
            model_name = await resolve_enterprise_cron_model(
                params.get("model_name"),
                group_id=group_id,
                bot_id=bot_id,
                user_id=user_id,
            )
            model_selection = None
            mcp = None
            credential_ref = ""
        else:
            group_id = normalize_tenant_id(params.get("group_id"))
            bot_id = normalize_tenant_id(params.get("bot_id"))
            user_id = normalize_tenant_id(params.get("user_id"))
            model_name, model_source = resolve_cron_model(
                params.get("model_name"),
                zen_entries=await self._zen_entries(),
            )
            model_selection = params.get("model_selection")
            if model_selection is not None:
                from gateway.common.model_selection import ModelSelection
                from gateway.common.model_routing_registry import ModelSelectionResolver
                selection = ModelSelection.model_validate(model_selection)
                ModelSelectionResolver().resolve(selection)
                model_selection = selection.model_dump()
            # 登录免费模型：把创建连接上的华为账号绑定到任务（凭据句柄）。路由
            # user_id 与登录账号是两套独立体系，不能互相推导；没有可用登录会话就
            # 明确拒绝（400），不留一个到点必失败的任务。自配模型（即使与登录模型
            # 同名）不绑定，执行侧凭据注入只认绑定，不会误切到登录模型计费。
            credential_ref = (
                _login_credential_ref_from_session(auth_session)
                if model_source == CRON_MODEL_SOURCE_LOGIN
                else ""
            )
            # mcp：会话级 MCP 选择，随 job 落库；调度执行时注入 chat.send 的
            # ``mcp`` 字段走 AgentServer 的 reconcile_session_mcp。只做类型
            # 规范化（strip/去空/去重），不校验存在性（断连后 job 应降级运行）。
            mcp = normalize_cron_job_mcp(params.get("mcp"))

        targets = self._normalize_targets(raw_targets)

        self._validate_schedule(cron_expr=cron_expr, timezone=timezone)
        description = self._normalize_description(description, name)
        if enterprise:
            wake_offset_seconds = self._clamp_wake_offset_for_upcoming_run(
                wake_offset_seconds,
                cron_expr=cron_expr,
                timezone=timezone,
            )

        routing_sid = self._routing_session_id(targets, params.get("session_id"))
        chat_type = params.get("chat_type")
        delete_after_run = params.get("delete_after_run")
        timeout_seconds = params.get("timeout_seconds")
        # work_mode 解析(严格校验:非法值由 resolve_request_work_mode 返回 BAD_REQUEST);
        # 默认值按 controller 目标通道推断(tui→code,web/未设置→work)
        from gateway.runtime.work_mode import (
            resolve_request_work_mode,
        )

        default_channel = (
            self._target_channel.value if self._target_channel is not None else "web"
        )
        work_mode, mode_err = resolve_request_work_mode(params, default_channel)
        if mode_err is not None:
            raise ValueError(f"invalid work_mode: {params.get('work_mode')!r}")
        # project_id / project_dir → project_id 解析(设计文档 §6.1 + work_mode 隔离)。
        # A3g：项目绑定统一由入口在目标 AgentServer 完成——web/tui 经
        # PROJECT_CRON_RESOLVE_BINDING，agent 工具经 server 侧 CronTools——
        # controller 把结果作为不透明值持久化，gateway 本地 project_store
        # 反查已删除。marker 缺失时仅放行无项目字段/默认项目的系统路径
        # （proactive.tick 等）；真实 project_id/project_dir 必须先经
        # AgentServer 解析，防止绑定被部署侧同名项目污染。
        raw_project_id = str(params.get("project_id") or "").strip()
        project_dir_raw = params.get("project_dir")
        project_dir_val = (
            str(project_dir_raw).strip()
            if isinstance(project_dir_raw, str) and project_dir_raw.strip()
            else ""
        )
        # 默认项目 ID（default/default_code）直接映射 work_mode，无需查表放行
        is_default_project = raw_project_id in (DEFAULT_PROJECT_ID_WORK, DEFAULT_PROJECT_ID_CODE)
        needs_agentos_resolution = bool(project_dir_val) or (
            bool(raw_project_id) and not is_default_project
        )
        if needs_agentos_resolution and not allow_unresolved_project_id:
            raise ValueError(
                "cron project binding must be resolved by the target AgentServer"
            )
        resolved_project_id = raw_project_id
        caller_work_mode = str(params.get("work_mode") or "").strip()
        if caller_work_mode in ("code", "work"):
            work_mode = caller_work_mode
        if work_mode not in EXECUTION_WORK_MODES:
            raise ValueError(f"cron is not supported for work_mode: {work_mode!r}")
        app_id = str(params.get("app_id") or "").strip()
        if not await self._scheduler.project_execution_allowed(
            resolved_project_id, user_id, group_id, bot_id
        ):
            raise ValueError("OPERATION_IN_PROGRESS: project execution is blocked")
        stored_wake_offset = (
            int(wake_offset_seconds) if wake_offset_seconds is not None else None
        )
        job = await self._store.create_job(
            job_id=str(params.get("id") or "").strip() or None,
            name=name,
            cron_expr=cron_expr,
            timezone=timezone,
            enabled=enabled,
            wake_offset_seconds=stored_wake_offset,
            description=description,
            targets=targets,
            session_id=routing_sid,
            chat_type=chat_type,
            mode=mode,
            delete_after_run=delete_after_run,
            timeout_seconds=timeout_seconds,
            project_id=resolved_project_id,
            model_name=model_name,
            model_selection=model_selection,
            mcp=mcp,
            app_id=app_id,
            work_mode=work_mode,
            user_id=user_id,
            group_id=group_id,
            bot_id=bot_id,
            credential_ref=credential_ref,
        )
        await self._scheduler.reload()
        return job.to_dict()

    @_serialize_mutation
    async def update_job(
        self,
        job_id: str,
        patch: dict[str, Any],
        *,
        user_id: str = "",
        group_id: str = "",
        bot_id: str = "",
    ) -> dict[str, Any]:
        patch = dict(patch or {})
        allow_unresolved_project_id = bool(
            patch.pop("_agentos_project_binding_verified", False)
        )
        # 登录会话 id（服务端专有键，见 create_job）——改选登录模型时重绑凭据。
        auth_session = str(patch.pop("_auth_session", "") or "").strip()
        # 身份只从函数参数核对，补丁改不了创建时定死的身份。
        patch = strip_sticky_identity_fields(patch)
        patch.pop("service_id", None)
        patch.pop("agent_id", None)
        existing = await self._store.get_job(job_id)
        if existing is None or not self._owns_job(
            existing, user_id=user_id, group_id=group_id, bot_id=bot_id
        ):
            raise KeyError("job not found")
        enterprise = is_enterprise()
        if "mode" in patch:
            patch["mode"] = normalize_cron_job_mode(patch.get("mode"))
            # 同 create_job：内部模式只随 proactive_cron_sync 注册，不允许把
            # 既有任务改写成 proactive.tick（#5128）。
            if patch["mode"] == "proactive.tick":
                raise ValueError(
                    "proactive.tick 是系统内部模式，由设置→主动推荐开关自动维护，不能手动修改"
                )
        if enterprise:
            from gateway.cron.enterprise_model import (
                resolve_enterprise_cron_model,
            )

            patch.pop("credential_ref", None)
            patch.pop("model_selection", None)
            patch.pop("mcp", None)
            if "model_name" in patch:
                patch["model_name"] = await resolve_enterprise_cron_model(
                    patch.get("model_name"),
                    group_id=str(group_id or existing.group_id or ""),
                    bot_id=str(bot_id or existing.bot_id or ""),
                    user_id=str(user_id or existing.user_id or ""),
                )
        elif "model_name" in patch:
            canonical, model_source = resolve_cron_model(
                patch.get("model_name"),
                zen_entries=await self._zen_entries(),
            )
            patch["model_name"] = canonical
            # 模型来源变化时同步重算绑定：改选登录模型 → 绑定当前连接的华为
            # 账号；换回自配模型/清除模型 → 解绑，否则执行侧仍会注入登录凭据，
            # 同名自配模型会被切到登录模型计费。
            if model_source == CRON_MODEL_SOURCE_LOGIN:
                patch["credential_ref"] = _login_credential_ref_from_session(
                    auth_session
                )
            else:
                patch["credential_ref"] = ""
        if not enterprise and "mcp" in patch:
            # 显式传 null/[] 归 None（清除选择，执行时回到全局默认集）。
            patch["mcp"] = normalize_cron_job_mcp(patch.get("mcp"))
        if "targets" in patch:
            patch["targets"] = self._normalize_targets(patch["targets"])
        if "cron_expr" in patch:
            patch["cron_expr"] = normalize_cron_expr(str(patch["cron_expr"]).strip())
        if "cron_expr" in patch or "timezone" in patch:
            cron_expr = str(patch.get("cron_expr") or existing.cron_expr).strip()
            timezone = str(patch.get("timezone") or existing.timezone).strip()
            self._validate_schedule(cron_expr=cron_expr, timezone=timezone)
        if enterprise and any(
            key in patch for key in ("wake_offset_seconds", "cron_expr", "timezone")
        ):
            upcoming_expr = str(patch.get("cron_expr") or existing.cron_expr).strip()
            upcoming_tz = str(patch.get("timezone") or existing.timezone).strip()
            current_offset = (
                patch["wake_offset_seconds"]
                if "wake_offset_seconds" in patch
                else getattr(existing, "wake_offset_seconds", 0)
            )
            patch["wake_offset_seconds"] = self._clamp_wake_offset_for_upcoming_run(
                current_offset,
                cron_expr=upcoming_expr,
                timezone=upcoming_tz or "Asia/Shanghai",
            )
        if "description" in patch:
            name = str(patch.get("name") or existing.name or "").strip()
            patch["description"] = self._normalize_description(
                str(patch.get("description") or ""), name
            )

        # work_mode / project_id / project_dir 重解析:
        # A3g：项目字段的重解析统一由入口经目标 AgentServer 完成（marker 标记），
        # gateway 本地 project_store 反查已删除。marker 缺失的 patch 不允许
        # 携带项目字段（必须先经 AgentServer 解析）；work_mode 单独 patch
        # 沿用原规则拒绝（仅能伴随项目重解析生效）。
        from gateway.common.work_mode import DEFAULT_WEB_WORK_MODE

        caller_work_mode = str(patch.get("work_mode") or "").strip()
        has_project_fields = "project_id" in patch or "project_dir" in patch
        patch_touches_binding = has_project_fields or "work_mode" in patch
        if patch_touches_binding and not allow_unresolved_project_id:
            if has_project_fields:
                raise ValueError(
                    "cron project binding must be resolved by the target AgentServer"
                )
            # 值本身可能合法，真正原因是 work_mode 不能脱离项目重解析单独 patch
            raise ValueError(
                "work_mode patch requires project re-resolution via target AgentServer"
            )
        patch["work_mode"] = (
            caller_work_mode
            if caller_work_mode in ("code", "work")
            else (existing.work_mode or DEFAULT_WEB_WORK_MODE)
        )

        final_targets = str(patch.get("targets") or existing.targets).strip()
        if "session_id" in patch:
            patch["session_id"] = self._routing_session_id(
                final_targets, patch.get("session_id")
            )
        elif "targets" in patch:
            patch["session_id"] = self._routing_session_id(
                final_targets, existing.session_id
            )

        if patch.get("enabled") or any(
            key in patch for key in ("project_id", "project_dir")
        ):
            if not await self._scheduler.project_execution_allowed(
                patch.get("project_id", existing.project_id),
                user_id or getattr(existing, "user_id", "") or "",
                group_id or getattr(existing, "group_id", "") or "",
                bot_id or getattr(existing, "bot_id", "") or "",
            ):
                raise ValueError("OPERATION_IN_PROGRESS: project execution is blocked")
        job = await self._store.update_job(job_id, patch)
        await self._scheduler.reload()
        return job.to_dict()

    async def delete_job(
        self,
        job_id: str,
        *,
        force: bool = False,
        delete_sessions: bool = True,
        user_id: str = "",
        group_id: str = "",
        bot_id: str = "",
        skip_ownership: bool = False,
    ) -> bool:
        existing = await self._store.get_job(job_id)
        if existing is None:
            return False
        if not skip_ownership and not self._owns_job(
            existing, user_id=user_id, group_id=group_id, bot_id=bot_id
        ):
            return False
        if not is_enterprise():
            if (
                not force
                and str(getattr(existing, "mode", "") or "").strip().lower() == "proactive.tick"
            ):
                return await self._store.delete_job(job_id)
            if delete_sessions:
                if existing.enabled:
                    await self._store.update_job(job_id, {"enabled": False})
                    await self._scheduler.reload()
                await self._scheduler.stop_job_runs(job_id)
                await self._scheduler.delete_cron_sessions(job_id, existing.user_id)
        deleted = await self._store.delete_job(job_id, force=force)
        if deleted:
            await self._scheduler.reload()
        return deleted

    async def toggle_job(
        self,
        job_id: str,
        enabled: bool,
        *,
        user_id: str = "",
        group_id: str = "",
        bot_id: str = "",
    ) -> dict[str, Any]:
        """开关走修改。打开时的项目闸门和三个身份核对都在 ``update_job`` 里。"""
        return await self.update_job(
            job_id,
            {"enabled": bool(enabled)},
            user_id=user_id,
            group_id=group_id,
            bot_id=bot_id,
        )

    async def preview_job(
        self,
        job_id: str,
        count: int = 5,
        *,
        user_id: str = "",
        group_id: str = "",
        bot_id: str = "",
    ) -> list[dict[str, Any]]:
        job = await self._store.get_job(job_id)
        if job is None or not self._owns_job(
            job, user_id=user_id, group_id=group_id, bot_id=bot_id
        ):
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
                _msg = str(exc)
                if "CroniterBadDateError" in _msg or "failed to find next date" in _msg:
                    break
                raise
            if out and push_dt.isoformat() == out[-1]["push_at"]:
                break
            wake_dt = push_dt - timedelta(
                seconds=max(0, int(job.wake_offset_seconds or 0))
            )
            out.append({"wake_at": wake_dt.isoformat(), "push_at": push_dt.isoformat()})
        return out

    async def run_now(
        self,
        job_id: str,
        *,
        user_id: str = "",
        group_id: str = "",
        bot_id: str = "",
    ) -> str:
        """立刻跑一次。个人版和企业版都先核对用户、群、机器人。

        没传的按 ``default``。任务不存在，或身份对不上，都当作找不到。
        通过之后才交给调度器。这里不查项目编号。
        """
        job = await self._store.get_job(job_id)
        if job is None or not self._matches_tenant(
            job, user_id=user_id, group_id=group_id, bot_id=bot_id
        ):
            raise KeyError("job not found")
        return await self._scheduler.trigger_run_now(job_id)

    async def run_now_info(self, job_id: str) -> dict[str, Any]:
        return await self._scheduler.trigger_run_now_info(job_id)

    def mark_permission_resume_succeeded(self, session_id: str) -> None:
        """人在定时任务会话里点完允许、这一轮也写完了：把「还在等」的记号抹掉。"""
        self._scheduler.mark_permission_resume_succeeded(session_id)
