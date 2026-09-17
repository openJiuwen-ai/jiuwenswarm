from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import portalocker

from jiuwenswarm.common.utils import get_cron_jobs_path
from jiuwenswarm.common.work_mode import DEFAULT_WEB_WORK_MODE
from jiuwenswarm.runtime.cron.cron_job_mutations import (
    _PROACTIVE_TICK_MODE,
    _ProactiveJobProtected,
    apply_cron_job_patch,
    build_new_cron_job,
    ensure_proactive_deletable,
    migrate_work_mode_on_items,
    parse_cron_jobs,
)
from jiuwenswarm.runtime.cron.models import CronJob, CronTarget

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
_FILE_LOCK_TIMEOUT_SEC = 10.0


class FileCronJobStore:
    """Persist cron jobs to ~/.jiuwenswarm/agent/home/cron_jobs.json.

    并发安全:
      - ``asyncio.Lock``：同进程协程互斥；
      - ``portalocker`` 伴生 ``cron_jobs.json.lock``：跨进程（多 Gateway / Agent）互斥。
      整个 read-modify-write 在双层锁内完成，避免 lost update。
    """

    supports_watch = False

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

    def _file_revision(self) -> int:
        try:
            stat = self._path.stat()
            return (
                (int(stat.st_mtime_ns) << 40)
                ^ (int(stat.st_ctime_ns) << 16)
                ^ int(stat.st_size)
            )
        except OSError:
            return 0

    async def get_revision(self) -> int:
        return self._file_revision()

    async def list_jobs(self) -> list[CronJob]:
        # 惰性迁移:在同一个锁内 read + 推断缺 work_mode 的老 job + writeback,
        # 替代启动迁移 ``migrate_legacy_jobs_at_startup``。
        def _body() -> list[CronJob]:
            data = self._read_json_unlocked()
            jobs_raw = data.get("jobs") or []
            if not isinstance(jobs_raw, list):
                return []

            jobs_raw, changed = migrate_work_mode_on_items(jobs_raw)
            if changed:
                data["jobs"] = jobs_raw
                try:
                    self._write_json_unlocked(data)
                except (OSError, ValueError, TypeError) as exc:
                    logger.warning(
                        "Cron 惰性迁移写回 cron_jobs.json 失败: %s", exc
                    )
            return parse_cron_jobs(jobs_raw)

        return await self._run_locked(_body)

    async def get_job(self, job_id: str) -> CronJob | None:
        job_id = str(job_id or "").strip()
        if not job_id:
            return None
        for job in await self.list_jobs():
            if job.id == job_id:
                return job
        return None

    @staticmethod
    def build_job(
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
        mcp: list[str] | None = None,
        app_id: str = "",
        work_mode: str = DEFAULT_WEB_WORK_MODE,
        user_id: str = "",
    ) -> CronJob:
        """Construct and validate a ``CronJob`` without persisting it.

        Gateway remains the single persistence owner.  Runtime cron tools use
        this builder to produce the same validated wire representation before
        submitting a mutation through the resident host.
        """
        return build_new_cron_job(
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
            mcp=mcp,
            app_id=app_id,
            work_mode=work_mode,
            user_id=user_id,
        )

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
        mcp: list[str] | None = None,
        app_id: str = "",
        work_mode: str = DEFAULT_WEB_WORK_MODE,
        user_id: str = "",
    ) -> CronJob:
        job = self.build_job(
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
            mcp=mcp,
            app_id=app_id,
            work_mode=work_mode,
            user_id=user_id,
        )
        await self._upsert_job(job)
        return job

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> CronJob:
        job_id = str(job_id or "").strip()
        if not job_id:
            raise ValueError("id is required")
        existing = await self.get_job(job_id)
        if existing is None:
            raise KeyError("job not found")
        updated = apply_cron_job_patch(existing, dict(patch or {}))
        await self._upsert_job(updated)
        return updated

    async def delete_job(self, job_id: str, *, force: bool = False) -> bool:
        job_id = str(job_id or "").strip()
        if not job_id:
            return False
        if not force:
            ensure_proactive_deletable(await self.get_job(job_id), force=False)

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

    async def watch(self, callback: Callable[[], Awaitable[None]]) -> None:
        del callback
        raise NotImplementedError("FileCronJobStore does not support etcd watch")

    async def aclose(self) -> None:
        return None

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


# Personal-edition / historical import path.
CronJobStore = FileCronJobStore

__all__ = [
    "FileCronJobStore",
    "CronJobStore",
    "CronJob",
    "_PROACTIVE_TICK_MODE",
    "_ProactiveJobProtected",
]
