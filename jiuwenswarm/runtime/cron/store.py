from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

import portalocker

from jiuwenswarm.runtime.cron.models import (
    CronJob,
    CronTarget,
    CRON_JOB_DEFAULT_MODE,
    normalize_cron_job_mode,
    normalize_cron_job_timeout_seconds,
)
from jiuwenswarm.common.utils import get_cron_jobs_path
from jiuwenswarm.common.work_mode import (
    DEFAULT_TUI_WORK_MODE,
    DEFAULT_WEB_WORK_MODE,
    normalize_work_mode,
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


class CronJobStore:
    """Persist cron jobs to ~/.jiuwenswarm/agent/home/cron_jobs.json.

    并发安全:
      - ``asyncio.Lock``：同进程协程互斥；
      - ``portalocker`` 伴生 ``cron_jobs.json.lock``：跨进程（多 Gateway）互斥。
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

    async def list_jobs(self) -> list[CronJob]:
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
                    if isinstance(existing_wm, str) and existing_wm.strip() in {
                        "code",
                        "work",
                    }:
                        continue
                    item["work_mode"] = _resolve_cron_job_work_mode(
                        item, id_to_work_mode
                    )
                    changed = True
                if changed:
                    try:
                        self._write_json_unlocked(data)
                    except (OSError, ValueError, TypeError) as exc:
                        logger.warning("Cron 惰性迁移写回 cron_jobs.json 失败: %s", exc)

            jobs: list[CronJob] = []
            for item in jobs_raw:
                if not isinstance(item, dict):
                    continue
                try:
                    jobs.append(CronJob.from_dict(item))
                except Exception as exc:  # noqa: BLE001
                    # Keep one corrupt entry from disabling the scheduler, but
                    # make the rejected job and reason observable to operators.
                    logger.warning(
                        "Ignoring invalid cron job id=%s: %s",
                        str(item.get("id") or ""),
                        exc,
                    )
                    continue
            return jobs

        jobs = await self._run_locked(_body)
        jobs.sort(
            key=lambda j: (j.updated_at or 0.0, j.created_at or 0.0), reverse=True
        )
        return jobs

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
        app_id: str = "",
        work_mode: str = DEFAULT_WEB_WORK_MODE,
        user_id: str = "",
    ) -> CronJob:
        """Construct and validate a ``CronJob`` without persisting it.

        Gateway remains the single persistence owner.  Runtime cron tools use
        this builder to produce the same validated wire representation before
        submitting a mutation through the resident host.
        """
        now = time.time()
        sid = (
            str(session_id).strip()
            if isinstance(session_id, str) and session_id.strip()
            else None
        )
        ct = (
            str(chat_type).strip()
            if isinstance(chat_type, str) and chat_type.strip()
            else None
        )
        m = (
            normalize_cron_job_mode(mode)
            if mode is not None and str(mode).strip()
            else CRON_JOB_DEFAULT_MODE
        )
        dar = bool(delete_after_run) if delete_after_run is not None else False
        timeout = (
            normalize_cron_job_timeout_seconds(timeout_seconds)
            if timeout_seconds is not None
            else None
        )
        pid = (
            str(project_id).strip()
            if isinstance(project_id, str) and project_id.strip()
            else ""
        )
        model_name_val = (
            str(model_name).strip()
            if isinstance(model_name, str) and model_name.strip()
            else None
        )
        job = CronJob(
            id=str(job_id or "").strip() or uuid.uuid4().hex,
            name=str(name or "").strip(),
            enabled=bool(enabled),
            cron_expr=str(cron_expr or "").strip(),
            timezone=str(timezone or "").strip(),
            wake_offset_seconds=int(wake_offset_seconds)
            if wake_offset_seconds is not None
            else 0,
            description=str(description or ""),
            targets=str(targets or "").strip(),
            session_id=sid,
            created_at=now,
            updated_at=now,
            chat_type=ct,
            mode=m,
            delete_after_run=dar,
            timeout_seconds=timeout,
            project_id=pid,
            model_name=model_name_val,
            app_id=str(app_id or "").strip(),
            work_mode=normalize_work_mode(work_mode, default=DEFAULT_WEB_WORK_MODE),
            user_id=str(user_id or "").strip(),
        )
        # validate via round-trip
        CronJob.from_dict(job.to_dict())
        return job

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
        patch = dict(patch or {})
        existing = await self.get_job(job_id)
        if existing is None:
            raise KeyError("job not found")

        # proactive.tick job：只接受调度字段（cron_expr/timezone），其余字段一律丢弃，
        # 防止前端或其它调用方改 name/enabled/description/wake_offset/targets/mode 等，
        # 这些字段由 config 开关 / proactive_cron_sync / scheduler 统一维护。
        if (
            str(getattr(existing, "mode", "") or "").strip().lower()
            == _PROACTIVE_TICK_MODE
        ):
            dropped = [k for k in patch if k not in _PROACTIVE_UPDATE_ALLOWED_KEYS]
            if dropped:
                logger.warning(
                    "[CronStore] reject proactive.tick update fields on job=%s: %s (only %s allowed)",
                    job_id,
                    ", ".join(dropped),
                    ", ".join(sorted(_PROACTIVE_UPDATE_ALLOWED_KEYS)),
                )
                patch = {
                    k: v
                    for k, v in patch.items()
                    if k in _PROACTIVE_UPDATE_ALLOWED_KEYS
                }

        updated = existing
        if "name" in patch:
            updated = replace(updated, name=str(patch.get("name") or "").strip())
        if "enabled" in patch:
            enabled_val = bool(patch.get("enabled"))
            updated = replace(updated, enabled=enabled_val)
            # Re-enabling a job implies it is no longer expired, unless caller explicitly sets expired.
            if enabled_val and "expired" not in patch:
                updated = replace(updated, expired=False)
        if "cron_expr" in patch:
            updated = replace(
                updated, cron_expr=str(patch.get("cron_expr") or "").strip()
            )
            # Editing schedule implies it is no longer expired, unless caller explicitly sets expired.
            if "expired" not in patch:
                updated = replace(updated, expired=False)
        if "timezone" in patch:
            updated = replace(
                updated, timezone=str(patch.get("timezone") or "").strip()
            )
        if "wake_offset_seconds" in patch:
            raw = patch.get("wake_offset_seconds")
            try:
                wos = int(raw)
            except Exception as exc:  # noqa: BLE001
                raise ValueError("wake_offset_seconds must be int") from exc
            updated = replace(updated, wake_offset_seconds=max(0, wos))
        if "description" in patch:
            updated = replace(updated, description=str(patch.get("description") or ""))
        if "targets" in patch:
            updated = replace(updated, targets=str(patch.get("targets") or "").strip())
        if "session_id" in patch:
            raw_sid = patch.get("session_id")
            new_sid = (
                str(raw_sid).strip()
                if isinstance(raw_sid, str) and str(raw_sid).strip()
                else None
            )
            updated = replace(updated, session_id=new_sid)
        if "chat_type" in patch:
            raw_ct = patch.get("chat_type")
            new_ct = (
                str(raw_ct).strip()
                if isinstance(raw_ct, str) and str(raw_ct).strip()
                else None
            )
            updated = replace(updated, chat_type=new_ct)
        if "expired" in patch:
            updated = replace(updated, expired=bool(patch.get("expired")))
        if "mode" in patch:
            updated = replace(updated, mode=normalize_cron_job_mode(patch.get("mode")))
        if "delete_after_run" in patch:
            updated = replace(
                updated, delete_after_run=bool(patch.get("delete_after_run"))
            )
        if "timeout_seconds" in patch:
            raw_timeout = patch.get("timeout_seconds")
            if raw_timeout is None:
                updated = replace(updated, timeout_seconds=None)
            else:
                updated = replace(
                    updated,
                    timeout_seconds=normalize_cron_job_timeout_seconds(raw_timeout),
                )
        if "project_id" in patch:
            raw_pid = patch.get("project_id")
            new_pid = (
                str(raw_pid).strip()
                if isinstance(raw_pid, str) and raw_pid.strip()
                else ""
            )
            updated = replace(updated, project_id=new_pid)
        if "last_session_id" in patch:
            raw_lsid = patch.get("last_session_id")
            new_lsid = (
                str(raw_lsid).strip()
                if isinstance(raw_lsid, str) and str(raw_lsid).strip()
                else None
            )
            updated = replace(updated, last_session_id=new_lsid)
        if "model_name" in patch:
            raw_model_name = patch.get("model_name")
            new_model_name = (
                str(raw_model_name).strip()
                if isinstance(raw_model_name, str) and str(raw_model_name).strip()
                else None
            )
            updated = replace(updated, model_name=new_model_name)
        if "work_mode" in patch:
            # work_mode 由 controller 从 project_dir + work_mode 重解析后注入,
            # 或由 project_id 变更时从 Project 记录注入。store 层仅做规范化写入。
            updated = replace(
                updated,
                work_mode=normalize_work_mode(
                    patch.get("work_mode"), default=DEFAULT_WEB_WORK_MODE
                ),
            )

        updated.updated_at = time.time()
        CronJob.from_dict(updated.to_dict())
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
                and str(getattr(existing, "mode", "") or "").strip().lower()
                == _PROACTIVE_TICK_MODE
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
