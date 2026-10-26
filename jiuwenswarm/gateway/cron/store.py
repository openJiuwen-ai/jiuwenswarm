from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import portalocker

from jiuwenswarm.gateway.cron.cron_job_mutations import (
    apply_cron_job_patch,
    build_new_cron_job,
    sort_cron_jobs,
)
from jiuwenswarm.gateway.cron.models import CronJob, CronTarget
from jiuwenswarm.common.utils import get_cron_jobs_path
from jiuwenswarm.common.work_mode import (
    DEFAULT_TUI_WORK_MODE,
    DEFAULT_WEB_WORK_MODE,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_FILE_LOCK_TIMEOUT_SEC = 10.0

# proactive.tick 是由 proactive_cron_sync 自动注册、由 config 开关驱动的任务。
# 其 name/enabled/description/wake_offset/targets/mode 均由系统/配置侧维护，
# update 时只允许改调度本身（cron_expr/timezone）；expired/updated_at 由调度器/内部写。
# 用 mode 判断（而非硬编码 id），避免依赖 id 字符串。
_PROACTIVE_TICK_MODE = "proactive.tick"
_PROACTIVE_UPDATE_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"cron_expr", "timezone", "expired", "updated_at"}
)


def _infer_work_mode_from_targets(job_item: dict[str, Any]) -> str:
    """按 job 的 targets.channel_id 推断 work_mode(迁移兜底,修复 C2)。

    当 project_id 反查失败(默认项目/不存在/list_projects 失败)时,按 targets 的
    channel_id 推断:
      - 含 tui 通道 → "code"(TUI 创建的 job 通常为 code 模式)
      - 其他 → "work"(Web/IM 等创建的 job 通常为 work 模式)

    支持两种 targets 格式:
      - 新格式 string: ``"tui"`` / ``"web"`` / ``"tui,web"`` 等
      - 旧格式 list[dict]: ``[{"channel_id": "tui"}]``
    """
    targets = job_item.get("targets")
    if isinstance(targets, str):
        # 新格式:逗号分隔的 channel_id 字符串
        for ch in targets.split(","):
            ch = ch.strip().lower()
            if ch == "tui":
                return DEFAULT_TUI_WORK_MODE
    elif isinstance(targets, list):
        # 旧格式:list of {channel_id, session_id?}
        for t in targets:
            if isinstance(t, dict):
                ch = str(t.get("channel_id") or "").strip().lower()
                if ch == "tui":
                    return DEFAULT_TUI_WORK_MODE
    return DEFAULT_WEB_WORK_MODE


def _build_cron_project_lookup() -> dict[str, str]:
    """构建 project_id → work_mode 映射,供 cron job 惰性迁移推断 work_mode。

    含隐藏项目(与 session 启动迁移一致):metadata 已有 project_id 直接命中时,
    即使项目已隐藏,继承其 work_mode 仍是最准确的归属。

    任何异常降级为空映射,``_resolve_cron_job_work_mode`` 会回退到
    ``_infer_work_mode_from_targets`` 按通道推断。
    """
    try:
        from jiuwenswarm.server.runtime.session.project_store import list_projects
        return {
            p.project_id: p.work_mode
            for p in list_projects(include_hidden=True, cache_bust=True)
            if p.project_id
        }
    except Exception:
        return {}


def _resolve_cron_job_work_mode(
    item: dict[str, Any], id_to_work_mode: dict[str, str]
) -> str:
    """为缺 work_mode 的老 cron job 推断 work_mode。

    规则(与原 ``migrate_legacy_jobs_at_startup`` 一致):
      1. project_id 命中真实 Project → 继承该 Project 的 work_mode;
      2. 未命中(默认项目/不存在/list_projects 失败)→
         按 targets.channel_id 推断(tui→code,其他→work)。
    """
    pid = str(item.get("project_id") or "").strip()
    if pid and pid in id_to_work_mode:
        return id_to_work_mode[pid]
    return _infer_work_mode_from_targets(item)


class _ProactiveJobProtected(RuntimeError):
    """proactive.tick job 受保护，禁止手动 删除/toggle/改非调度字段 时抛出。

    所有删除路径（web handler / TUI /cron / 自然语言 cron 工具）共用 store 层，
    在此抛出可统一拦截，避免 config 开关与 cron store 不一致。
    """


class FileCronJobStore:
    """Persist cron jobs to ~/.jiuwenswarm/agent/home/cron_jobs.json.

    并发安全:
      - ``asyncio.Lock``：同进程协程互斥；
      - ``portalocker`` 伴生 ``cron_jobs.json.lock``：跨进程（多 Gateway / Agent）互斥。
      整个 read-modify-write 在双层锁内完成，避免 lost update。
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        file_lock_timeout: float = _FILE_LOCK_TIMEOUT_SEC,
    ) -> None:
        self._path = path or get_cron_jobs_path()
        self._lock = asyncio.Lock()
        self._file_lock_timeout = float(file_lock_timeout)

    @property
    def path(self) -> Path:
        return self._path

    def _call_under_file_lock(self, fn: Callable[[], _T]) -> _T:
        """在伴生 ``cron_jobs.json.lock`` 上拿跨进程锁后执行 fn（不被原子 replace 覆盖）。

        须在进程内 ``asyncio.Lock`` 之内、经 ``to_thread`` 调用，避免阻塞事件循环。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with portalocker.Lock(str(lock_path), timeout=self._file_lock_timeout):
            return fn()

    async def _run_locked(self, fn: Callable[[], _T]) -> _T:
        """同进程协程串行 + 跨进程文件锁；文件锁等待放到线程池，避免阻塞事件循环。"""
        async with self._lock:
            return await asyncio.to_thread(self._call_under_file_lock, fn)

    async def list_jobs(self, *, filters: dict[str, Any] | None = None) -> list[CronJob]:
        # 惰性迁移:在同一个锁内 read + 推断缺 work_mode 的老 job + writeback,
        # 替代启动迁移 ``migrate_legacy_jobs_at_startup``。
        # 已迁移过的系统 jobs 全部 work_mode 合法,``needs_migration=False``
        # 直接跳过 lookup 与 writeback,零额外开销。
        def _body() -> list[CronJob]:
            data = self._read_json_unlocked()
            jobs_raw = data.get("jobs") or []
            if not isinstance(jobs_raw, list):
                return []

            # 第一遍:检测是否有 job 缺 work_mode(快速短路,避免无谓构建 lookup)
            needs_migration = False
            for item in jobs_raw:
                if not isinstance(item, dict):
                    continue
                existing_wm = item.get("work_mode")
                if not (
                    isinstance(existing_wm, str)
                    and existing_wm.strip() in {"code", "work"}
                ):
                    needs_migration = True
                    break

            if needs_migration:
                id_to_work_mode = _build_cron_project_lookup()
                changed = False
                for item in jobs_raw:
                    if not isinstance(item, dict):
                        continue
                    existing_wm = item.get("work_mode")
                    if (
                        isinstance(existing_wm, str)
                        and existing_wm.strip() in {"code", "work"}
                    ):
                        continue
                    item["work_mode"] = _resolve_cron_job_work_mode(
                        item, id_to_work_mode
                    )
                    changed = True
                if changed:
                    try:
                        self._write_json_unlocked(data)
                    except (OSError, ValueError, TypeError) as exc:
                        logger.warning(
                            "Cron 惰性迁移写回 cron_jobs.json 失败: %s", exc
                        )

            jobs: list[CronJob] = []
            for item in jobs_raw:
                if not isinstance(item, dict):
                    continue
                try:
                    jobs.append(CronJob.from_dict(item))
                except Exception:
                    # Ignore invalid entries to keep system robust
                    continue
            return jobs

        jobs = sort_cron_jobs(await self._run_locked(_body))
        # 与 CronJobRepository / GatewayDbCronJobStore 对齐：企业路径会传
        # filters={group_id, bot_id, user_id}，文件 store 无 DB 列过滤，内存过滤。
        raw_filters = filters or {}
        route_filters: dict[str, str] = {}
        for key in ("group_id", "bot_id", "user_id"):
            val = raw_filters.get(key)
            if isinstance(val, str) and val.strip():
                route_filters[key] = val.strip()
        if not route_filters:
            return jobs
        matched: list[CronJob] = []
        for job in jobs:
            if all(getattr(job, key, None) == value for key, value in route_filters.items()):
                matched.append(job)
        return matched

    async def get_job(self, job_id: str) -> CronJob | None:
        job_id = str(job_id or "").strip()
        if not job_id:
            return None
        for job in await self.list_jobs():
            if job.id == job_id:
                return job
        return None

    async def create_job(
        self,
        *,
        job_id: str | None = None,
        name: str,
        cron_expr: str,
        timezone: str,
        description: str,
        targets: str,
        enabled: bool = True,
        wake_offset_seconds: int | None = None,
        session_id: str | None = None,
        chat_type: str | None = None,
        mode: str | None = None,
        delete_after_run: bool | None = None,
        timeout_seconds: int | None = None,
        project_id: str = "",
        model_name: str | None = None,
        app_id: str = "",
        work_mode: str = DEFAULT_WEB_WORK_MODE,
        service_id: str | None = None,
        agent_id: str | None = None,
        group_id: str | None = None,
        bot_id: str | None = None,
        user_id: str | None = None,
    ) -> CronJob:
        job = build_new_cron_job(
            job_id=job_id,
            name=name,
            cron_expr=cron_expr,
            timezone=timezone,
            description=description,
            targets=targets,
            enabled=enabled,
            wake_offset_seconds=wake_offset_seconds,
            session_id=session_id,
            chat_type=chat_type,
            mode=mode,
            delete_after_run=delete_after_run,
            timeout_seconds=timeout_seconds,
            project_id=project_id,
            model_name=model_name,
            app_id=app_id,
            work_mode=work_mode,
            group_id=group_id,
            bot_id=bot_id,
            user_id=user_id,
        )
        tenant_sid = str(service_id or "default").strip() or "default"
        tenant_aid = str(agent_id or "default").strip() or "default"
        job = replace(job, service_id=tenant_sid, agent_id=tenant_aid)
        await self._upsert_job(job)
        return job

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> CronJob:
        job_id = str(job_id or "").strip()
        if not job_id:
            raise ValueError("id is required")
        patch = dict(patch or {})
        existing = await self.get_job(job_id)
        if existing is None:
            raise KeyError("job not found")

        # proactive.tick job：只接受调度字段（cron_expr/timezone），其余字段一律丢弃，
        # 防止前端或其它调用方改 name/enabled/description/wake_offset/targets/mode 等，
        # 这些字段由 config 开关 / proactive_cron_sync / scheduler 统一维护。
        if str(getattr(existing, "mode", "") or "").strip().lower() == _PROACTIVE_TICK_MODE:
            dropped = [k for k in patch if k not in _PROACTIVE_UPDATE_ALLOWED_KEYS]
            if dropped:
                logger.warning(
                    "[CronStore] reject proactive.tick update fields on job=%s: %s (only %s allowed)",
                    job_id, ", ".join(dropped), ", ".join(sorted(_PROACTIVE_UPDATE_ALLOWED_KEYS)),
                )
                patch = {k: v for k, v in patch.items() if k in _PROACTIVE_UPDATE_ALLOWED_KEYS}

        updated = apply_cron_job_patch(existing, patch)
        await self._upsert_job(updated)
        return updated

    async def delete_job(self, job_id: str, *, force: bool = False) -> bool:
        job_id = str(job_id or "").strip()
        if not job_id:
            return False
        # proactive.tick job 由主动推荐开关自动创建/删除，禁止任何路径
        # （web 面板 / TUI /cron / 自然语言 cron 工具）手动删除——否则会出现
        # config 开关仍开但 job 没了的不一致，且重启后会被 sync 重建。
        # force=True 仅供 proactive_cron_sync 在 config 开关关闭时合法删除用。
        if not force:
            existing = await self.get_job(job_id)
            if (
                    existing is not None
                    and str(getattr(existing, "mode", "") or "").strip().lower() == _PROACTIVE_TICK_MODE
            ):
                raise _ProactiveJobProtected(
                    "主动推荐定时任务由设置→主动推荐开关控制，不能删除；请到设置关闭开关。"
                )

        def _body() -> bool:
            data = self._read_json_unlocked()
            jobs_raw = data.get("jobs") or []
            if not isinstance(jobs_raw, list):
                jobs_raw = []
            kept: list[dict[str, Any]] = []
            deleted = False
            for item in jobs_raw:
                if not isinstance(item, dict):
                    continue
                if str(item.get("id") or "").strip() == job_id:
                    deleted = True
                    continue
                kept.append(item)
            data["version"] = int(data.get("version") or 1)
            data["jobs"] = kept
            if deleted:
                self._write_json_unlocked(data)
            return deleted

        return await self._run_locked(_body)

    async def _upsert_job(self, job: CronJob) -> None:
        def _body() -> None:
            data = self._read_json_unlocked()
            jobs_raw = data.get("jobs") or []
            if not isinstance(jobs_raw, list):
                jobs_raw = []
            out: list[dict[str, Any]] = []
            found = False
            for item in jobs_raw:
                if not isinstance(item, dict):
                    continue
                if str(item.get("id") or "").strip() == job.id:
                    out.append(job.to_dict())
                    found = True
                else:
                    out.append(item)
            if not found:
                out.append(job.to_dict())
            data["version"] = int(data.get("version") or 1)
            data["jobs"] = out
            self._write_json_unlocked(data)

        await self._run_locked(_body)

    async def upsert_from_dict(self, data: dict[str, Any]) -> CronJob:
        """Insert or replace a job from a serialized dict (mirror sync)."""
        job = CronJob.from_dict(dict(data))
        await self._upsert_job(job)
        return job

    async def _read_json(self) -> dict[str, Any]:
        return await self._run_locked(self._read_json_unlocked)

    def _read_json_unlocked(self) -> dict[str, Any]:
        path = self._path
        try:
            if not path.exists():
                return {"version": 1, "jobs": []}
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                return {"version": 1, "jobs": []}
            if "version" not in data:
                data["version"] = 1
            if "jobs" not in data:
                data["jobs"] = []
            return data
        except Exception:
            return {"version": 1, "jobs": []}

    def _write_json_unlocked(self, data: dict[str, Any]) -> None:
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)

    async def get_revision(self) -> int:
        """File mtime as microsecond revision; 0 if missing (supports delete detection)."""
        path = self._path
        try:
            if not path.exists():
                return 0
            return int(path.stat().st_mtime * 1_000_000)
        except OSError:
            return 0

    @staticmethod
    def _normalize_targets(targets: Any) -> list[CronTarget]:
        out: list[CronTarget] = []
        if isinstance(targets, list):
            for item in targets:
                if isinstance(item, CronTarget):
                    out.append(item)
                elif isinstance(item, dict):
                    out.append(CronTarget.from_dict(item))
        if not out:
            raise ValueError("targets is required")
        return out


CronJobStore = FileCronJobStore
