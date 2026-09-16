"""Shared CronJob construct / patch helpers used by file and etcd stores."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import replace
from typing import Any

from jiuwenswarm.common.work_mode import (
    DEFAULT_TUI_WORK_MODE,
    DEFAULT_WEB_WORK_MODE,
    normalize_work_mode,
)
from jiuwenswarm.gateway.cron.models import (
    CRON_JOB_DEFAULT_MODE,
    CronJob,
    normalize_cron_job_mode,
    normalize_cron_job_timeout_seconds,
)

logger = logging.getLogger(__name__)

# proactive.tick 是由 proactive_cron_sync 自动注册、由 config 开关驱动的任务。
# 其 name/enabled/description/wake_offset/targets/mode 均由系统/配置侧维护，
# update 时只允许改调度本身（cron_expr/timezone）；expired/updated_at 由调度器/内部写。
_PROACTIVE_TICK_MODE = "proactive.tick"
_PROACTIVE_UPDATE_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"cron_expr", "timezone", "expired", "updated_at"}
)


class _ProactiveJobProtected(RuntimeError):
    """proactive.tick job 受保护，禁止手动 删除/toggle/改非调度字段 时抛出。

    所有删除路径（web handler / TUI /cron / 自然语言 cron 工具）共用 store 层，
    在此抛出可统一拦截，避免 config 开关与 cron store 不一致。
    """


def sort_cron_jobs(jobs: list[CronJob]) -> list[CronJob]:
    jobs.sort(key=lambda j: (j.updated_at or 0.0, j.created_at or 0.0), reverse=True)
    return jobs


def job_dict_needs_work_mode(item: dict[str, Any]) -> bool:
    existing_wm = item.get("work_mode")
    return not (
        isinstance(existing_wm, str) and existing_wm.strip() in {"code", "work"}
    )


def infer_work_mode_from_targets(job_item: dict[str, Any]) -> str:
    """按 job 的 targets.channel_id 推断 work_mode(迁移兜底)。

    当 project_id 反查失败(默认项目/不存在/list_projects 失败)时,按 targets 的
    channel_id 推断:
      - 含 tui 通道 → "code"(TUI 创建的 job 通常为 code 模式)
      - 其他 → "work"(Web/IM 等创建的 job 通常为 work 模式)
    """
    targets = job_item.get("targets")
    if isinstance(targets, str):
        for ch in targets.split(","):
            ch = ch.strip().lower()
            if ch == "tui":
                return DEFAULT_TUI_WORK_MODE
    elif isinstance(targets, list):
        for t in targets:
            if isinstance(t, dict):
                ch = str(t.get("channel_id") or "").strip().lower()
                if ch == "tui":
                    return DEFAULT_TUI_WORK_MODE
    return DEFAULT_WEB_WORK_MODE


def build_cron_project_lookup() -> dict[str, str]:
    """构建 project_id → work_mode 映射,供 cron job 惰性迁移推断 work_mode。"""
    try:
        from jiuwenswarm.server.runtime.session.project_store import list_projects

        return {
            p.project_id: p.work_mode
            for p in list_projects(include_hidden=True, cache_bust=True)
            if p.project_id
        }
    except Exception:
        return {}


def resolve_cron_job_work_mode(
    item: dict[str, Any], id_to_work_mode: dict[str, str]
) -> str:
    pid = str(item.get("project_id") or "").strip()
    if pid and pid in id_to_work_mode:
        return id_to_work_mode[pid]
    return infer_work_mode_from_targets(item)


def migrate_work_mode_on_items(jobs_raw: list[Any]) -> tuple[list[Any], bool]:
    """Fill missing work_mode on raw job dicts. Returns (items, changed)."""
    needs_migration = False
    for item in jobs_raw:
        if isinstance(item, dict) and job_dict_needs_work_mode(item):
            needs_migration = True
            break
    if not needs_migration:
        return jobs_raw, False
    id_to_work_mode = build_cron_project_lookup()
    changed = False
    for item in jobs_raw:
        if not isinstance(item, dict) or not job_dict_needs_work_mode(item):
            continue
        item["work_mode"] = resolve_cron_job_work_mode(item, id_to_work_mode)
        changed = True
    return jobs_raw, changed


def parse_cron_jobs(jobs_raw: list[Any]) -> list[CronJob]:
    jobs: list[CronJob] = []
    for item in jobs_raw:
        if not isinstance(item, dict):
            continue
        try:
            jobs.append(CronJob.from_dict(item))
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[Cron] skip invalid job id=%s: %s",
                item.get("id"),
                exc,
            )
            continue
    return sort_cron_jobs(jobs)


def build_new_cron_job(
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
    """Construct and validate a ``CronJob`` without persisting it."""
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
        wake_offset_seconds=int(wake_offset_seconds) if wake_offset_seconds is not None else 0,
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
    CronJob.from_dict(job.to_dict())
    return job


def filter_proactive_update_patch(existing: CronJob, patch: dict[str, Any]) -> dict[str, Any]:
    if str(getattr(existing, "mode", "") or "").strip().lower() != _PROACTIVE_TICK_MODE:
        return patch
    dropped = [k for k in patch if k not in _PROACTIVE_UPDATE_ALLOWED_KEYS]
    if dropped:
        logger.warning(
            "[CronStore] reject proactive.tick update fields on job=%s: %s (only %s allowed)",
            existing.id,
            ", ".join(dropped),
            ", ".join(sorted(_PROACTIVE_UPDATE_ALLOWED_KEYS)),
        )
        return {k: v for k, v in patch.items() if k in _PROACTIVE_UPDATE_ALLOWED_KEYS}
    return patch


def ensure_proactive_deletable(existing: CronJob | None, *, force: bool) -> None:
    if force or existing is None:
        return
    if str(getattr(existing, "mode", "") or "").strip().lower() == _PROACTIVE_TICK_MODE:
        raise _ProactiveJobProtected(
            "主动推荐定时任务由设置→主动推荐开关控制，不能删除；请到设置关闭开关。"
        )


def apply_cron_job_patch(existing: CronJob, patch: dict[str, Any]) -> CronJob:
    patch = filter_proactive_update_patch(existing, dict(patch or {}))
    updated = existing
    if "name" in patch:
        updated = replace(updated, name=str(patch.get("name") or "").strip())
    if "enabled" in patch:
        enabled_val = bool(patch.get("enabled"))
        updated = replace(updated, enabled=enabled_val)
        if enabled_val and "expired" not in patch:
            updated = replace(updated, expired=False)
    if "cron_expr" in patch:
        updated = replace(updated, cron_expr=str(patch.get("cron_expr") or "").strip())
        if "expired" not in patch:
            updated = replace(updated, expired=False)
    if "timezone" in patch:
        updated = replace(updated, timezone=str(patch.get("timezone") or "").strip())
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
        updated = replace(updated, delete_after_run=bool(patch.get("delete_after_run")))
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
        new_pid = str(raw_pid).strip() if isinstance(raw_pid, str) and raw_pid.strip() else ""
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
        updated = replace(
            updated,
            work_mode=normalize_work_mode(patch.get("work_mode"), default=DEFAULT_WEB_WORK_MODE),
        )

    updated.updated_at = time.time()
    CronJob.from_dict(updated.to_dict())
    return updated
