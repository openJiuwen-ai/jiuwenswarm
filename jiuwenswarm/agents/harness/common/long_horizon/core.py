# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Core long-horizon primitives: schedule, briefing, prompts, events, store."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jiuwenswarm.agents.harness.common.long_horizon.models import (
    LongHorizonStage,
    LongHorizonTask,
    new_stage_id,
    touch_iso,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

STAGE_START_PROMPT = """\
[长程任务阶段执行]
任务：{title}
当前阶段：{stage_title}

本阶段执行计划（规划时已锁定，请按此执行）：
{plan}

任务简报：
{brief}

阶段进度：
{progress}

{context}

用户已通过「现在做」进入本阶段执行会话（当前阶段已是进行中）。
请只推进「当前阶段」交付；不要对其他阶段调用 long_horizon_task 的 start。
完成本阶段并经用户确认后，用 stage_action=done（带 stage_id）收口即可；同会话历史已足够，不必再写 conclusion。
缺少关键材料时最多提出三个具体问题。不得编造数据，也不得声称已完成未经用户确认的对外动作。
"""

_TITLE_META_SUFFIX = re.compile(
    r"[（(]\s*(?:[一二三四五六七八九十两\d]+\s*)?阶段"
    r"(?:推进|计划|安排|流程)?\s*[）)]\s*$"
)


def sanitize_task_title(title: str) -> str:
    """Keep the topic name; strip meta suffixes like「（五阶段推进）」."""
    text = " ".join(str(title or "").split()).strip()
    while True:
        cleaned = _TITLE_META_SUFFIX.sub("", text).strip(" -—_|/")
        if cleaned == text:
            break
        text = cleaned
    return text

# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------


class ScheduleError(ValueError):
    pass


def _zoneinfo(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(timezone or "").strip())
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ScheduleError("invalid_timezone") from exc


def compute_anchor_datetime(
    *,
    month: int,
    day: int,
    timezone: str = "Asia/Shanghai",
    hour: int = 9,
    minute: int = 0,
    now: datetime | None = None,
) -> datetime:
    tz = _zoneinfo(timezone)
    try:
        base = now.astimezone(tz) if now else datetime.now(tz)
        candidate = datetime(base.year, int(month), int(day), hour, minute, tzinfo=tz)
        if candidate <= base:
            candidate = candidate.replace(year=base.year + 1)
        return candidate
    except (TypeError, ValueError) as exc:
        raise ScheduleError("invalid_anchor_date") from exc


def coerce_stage_specs(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw.strip()) if raw.strip() else []
        except json.JSONDecodeError as exc:
            raise ScheduleError("invalid_stages") from exc
    if isinstance(raw, dict):
        inner = raw.get("stages")
        raw = inner if isinstance(inner, list) else [raw]
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ScheduleError("invalid_stages")
    return raw


def _coerce_clock(
    item: dict[str, Any],
    *,
    default_hour: int,
    default_minute: int,
) -> tuple[int, int]:
    """Resolve per-stage clock time; fall back to the task-level default."""
    hour = default_hour
    minute = default_minute

    time_raw = item.get("time") or item.get("clock") or item.get("at")
    if time_raw is not None and str(time_raw).strip() != "":
        text = str(time_raw).strip()
        try:
            if ":" in text:
                hour_text, minute_text = text.split(":", 1)
                hour = int(hour_text)
                minute = int(minute_text.split(":")[0])
            else:
                hour = int(text)
                minute = 0
        except (TypeError, ValueError) as exc:
            raise ScheduleError("invalid_stage_time") from exc

    if item.get("hour") is not None and str(item.get("hour")).strip() != "":
        try:
            hour = int(item["hour"])
        except (TypeError, ValueError) as exc:
            raise ScheduleError("invalid_stage_time") from exc
    if item.get("minute") is not None and str(item.get("minute")).strip() != "":
        try:
            minute = int(item["minute"])
        except (TypeError, ValueError) as exc:
            raise ScheduleError("invalid_stage_time") from exc

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError("invalid_stage_time")
    return hour, minute


def _parse_stage_datetime(
    item: dict[str, Any],
    *,
    anchor: datetime,
    hour: int,
    minute: int,
) -> datetime:
    """Resolve a stage due datetime.

    Prefer absolute ``due_at`` / ``date`` (with year). Legacy month/day specs are
    resolved relative to ``anchor`` (a calendar day later than the anchor belongs
    to the preceding year).
    """
    stage_hour, stage_minute = _coerce_clock(
        item, default_hour=hour, default_minute=minute
    )
    tz = anchor.tzinfo

    due_at = str(item.get("due_at") or "").strip()
    if due_at:
        try:
            text = due_at.replace("Z", "+00:00")
            # Allow "YYYY-MM-DD HH:MM" as well as ISO "YYYY-MM-DDTHH:MM".
            if "T" not in text and " " in text and text.count("-") >= 2:
                text = text.replace(" ", "T", 1)
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ScheduleError("invalid_stage_date") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=tz)
        # Date-only ISO ("2026-10-08") → apply default / stage clock.
        date_only = len(due_at) <= 10
        if date_only:
            date_only = "T" not in due_at and " " not in due_at
        midnight = parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0
        if date_only and midnight:
            parsed = parsed.replace(hour=stage_hour, minute=stage_minute, second=0)
        return parsed.astimezone(tz)

    date_raw = str(item.get("date") or item.get("due") or "").strip()
    if date_raw:
        try:
            text = date_raw.replace("Z", "+00:00")
            if "T" not in text and " " in text and text.count("-") >= 2:
                text = text.replace(" ", "T", 1)
            if "T" in text or "+" in text[10:] or text.endswith("Z"):
                parsed = datetime.fromisoformat(text)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=tz)
                return parsed.astimezone(tz)
            year_s, month_s, day_s = date_raw.split("-", 2)
            return datetime(
                int(year_s),
                int(month_s),
                int(day_s),
                stage_hour,
                stage_minute,
                tzinfo=tz,
            )
        except (TypeError, ValueError) as exc:
            raise ScheduleError("invalid_stage_date") from exc

    if item.get("month") is None or item.get("day") is None:
        raise ScheduleError("stage_date_required")
    try:
        month = int(item["month"])
        day = int(item["day"])
        anchor_key = anchor.month * 32 + anchor.day
        stage_key = month * 32 + day
        year = anchor.year - 1 if stage_key > anchor_key else anchor.year
        return datetime(
            year, month, day, stage_hour, stage_minute, tzinfo=tz
        )
    except (TypeError, ValueError) as exc:
        raise ScheduleError("invalid_stage_date") from exc


def stages_from_specs(
    specs: Any,
    *,
    task: LongHorizonTask,
    hour: int = 9,
    minute: int = 0,
    now: datetime | None = None,
) -> list[LongHorizonStage]:
    rows = coerce_stage_specs(specs)
    if not rows:
        raise ScheduleError("stages_required")
    anchor = compute_anchor_datetime(
        month=task.anchor.month,
        day=task.anchor.day or 1,
        timezone=task.anchor.timezone,
        hour=hour,
        minute=minute,
        now=now,
    )
    stages: list[LongHorizonStage] = []
    for item in rows:
        title = str(item.get("title") or "").strip()
        if not title:
            raise ScheduleError("stage_title_required")
        stage_at = _parse_stage_datetime(
            item, anchor=anchor, hour=hour, minute=minute
        )
        offset = (stage_at.date() - anchor.date()).days
        stages.append(
            LongHorizonStage(
                id=str(item.get("id") or "").strip() or new_stage_id(),
                offset_days=offset,
                title=title,
                hint=str(item.get("hint") or "").strip(),
                plan=str(item.get("plan") or "").strip(),
                status=str(item.get("status") or "pending"),  # type: ignore[arg-type]
                due_at=stage_at.isoformat(),
            )
        )
    return stages


_PROTECTED_STAGE_STATUSES = frozenset({"done", "skipped", "in_progress"})


def fill_stage_due_dates(
    task: LongHorizonTask,
    *,
    hour: int = 9,
    minute: int = 0,
    now: datetime | None = None,
    skip_statuses: frozenset[str] | None = None,
) -> LongHorizonTask:
    """Fill / normalize stage due times.

    Same-calendar-day stages whose planned clock time has already passed are
    bumped to ``now + 5 minutes``, so "today" stages remain creatable after a
    short confirmation pause. Strictly earlier calendar days still raise
    ``stage_in_past``.
    """
    skip = skip_statuses or frozenset()
    tz = _zoneinfo(task.anchor.timezone)
    base = now.astimezone(tz) if now else datetime.now(tz)
    anchor = compute_anchor_datetime(
        month=task.anchor.month,
        day=task.anchor.day or 1,
        timezone=task.anchor.timezone,
        hour=hour,
        minute=minute,
        now=base,
    )
    for stage in task.stages:
        if stage.status in skip:
            continue
        if stage.due_at:
            try:
                due = datetime.fromisoformat(stage.due_at)
            except ValueError as exc:
                raise ScheduleError("invalid_stage_date") from exc
            if due.tzinfo is None or due.utcoffset() is None:
                raise ScheduleError("invalid_stage_date")
            due = due.astimezone(tz)
        else:
            due = anchor + timedelta(days=stage.offset_days)
        if due <= base:
            if due.date() == base.date():
                due = base + timedelta(minutes=5)
            else:
                raise ScheduleError("stage_in_past")
        stage.due_at = due.isoformat()
    return task


def rewrite_task_stages(
    task: LongHorizonTask,
    specs: Any,
    *,
    hour: int = 9,
    minute: int = 0,
    now: datetime | None = None,
) -> LongHorizonTask:
    """Replace the open stage list while preserving protected runtime state.

    Agent passes a full desired ``stages`` list (same shape as draft). Matching
    ``id`` keeps ``done`` / ``skipped`` / ``in_progress``; omitted protected
    stages are auto-kept at the front. ``task.id`` / ``exec_session_id`` are
    never touched here.
    """
    existing_by_id = {stage.id: stage for stage in task.stages}
    rebuilt = stages_from_specs(
        specs, task=task, hour=hour, minute=minute, now=now
    )
    used_ids: set[str] = set()
    out: list[LongHorizonStage] = []
    for stage in rebuilt:
        old = existing_by_id.get(stage.id)
        if old is not None:
            used_ids.add(old.id)
            if old.status in _PROTECTED_STAGE_STATUSES:
                stage.status = old.status
                stage.conclusion = old.conclusion
                stage.due_at = old.due_at
                stage.hint = old.hint
                stage.snooze_until = ""
                if old.status in ("done", "skipped"):
                    stage.title = old.title
                    stage.plan = old.plan or stage.plan
                else:
                    stage.plan = stage.plan or old.plan
            elif old.status == "due":
                stage.status = "due"
                stage.snooze_until = ""
                stage.conclusion = ""
            else:
                stage.status = "pending"
                stage.snooze_until = ""
                stage.conclusion = ""
        else:
            stage.status = "pending"
            stage.snooze_until = ""
            stage.conclusion = ""
        stage.cron_job_id = ""
        out.append(stage)

    orphans = [
        stage
        for stage in task.stages
        if stage.id not in used_ids and stage.status in _PROTECTED_STAGE_STATUSES
    ]
    for stage in orphans:
        stage.cron_job_id = ""
    task.stages = orphans + out
    return fill_stage_due_dates(
        task,
        hour=hour,
        minute=minute,
        now=now,
        skip_statuses=_PROTECTED_STAGE_STATUSES,
    )


def draft_long_horizon_task(
    *,
    title: str,
    month: int,
    day: int,
    stages: Any,
    kind: str = "generic",
    recurrence: str = "once",
    timezone: str = "Asia/Shanghai",
    hour: int = 9,
    minute: int = 0,
    now: datetime | None = None,
) -> LongHorizonTask:
    rows = coerce_stage_specs(stages)
    if not rows:
        raise ScheduleError("stages_required")
    task = LongHorizonTask.new(
        title=sanitize_task_title(title),
        kind=kind if kind in ("report", "meeting", "generic") else "generic",  # type: ignore[arg-type]
        month=month,
        day=day,
        recurrence=recurrence if recurrence in ("yearly", "once") else "once",  # type: ignore[arg-type]
        timezone=timezone,
    )
    task.stages = stages_from_specs(
        rows, task=task, hour=hour, minute=minute, now=now
    )
    return fill_stage_due_dates(task, hour=hour, minute=minute, now=now)


# ---------------------------------------------------------------------------
# briefing
# ---------------------------------------------------------------------------

_STATUS_LABEL = {
    "pending": "未到",
    "due": "到点",
    "in_progress": "进行中",
    "snoozed": "稍后",
    "done": "已完成",
    "skipped": "已跳过",
}


def clip(text: str, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def format_due_at(
    when: datetime,
    *,
    timezone: str = "Asia/Shanghai",
) -> str:
    """Serialize a due time in the task timezone (never bare UTC Z)."""
    tz = _zoneinfo(timezone)
    local = when.astimezone(tz).replace(microsecond=0)
    return local.isoformat()


def shift_due_at(
    *,
    timezone: str = "Asia/Shanghai",
    hours: float = 0,
    days: float = 0,
    base: datetime | None = None,
) -> str:
    """Return ``base|now + delta`` as a timezone-aware due_at string."""
    tz = _zoneinfo(timezone)
    if base is None:
        start = datetime.now(tz)
    elif base.tzinfo is None or base.utcoffset() is None:
        start = base.replace(tzinfo=tz)
    else:
        start = base.astimezone(tz)
    return format_due_at(
        start + timedelta(hours=hours, days=days),
        timezone=timezone,
    )


def require_brief(task: LongHorizonTask, incoming: str = "") -> str:
    text = str(incoming or task.brief or "").strip()
    if not text:
        raise ValueError("brief_required")
    return clip(text, 800)


def require_stage_plans(task: LongHorizonTask) -> None:
    if any(not str(stage.plan or "").strip() for stage in task.stages):
        raise ValueError("stage_plan_required")
    for stage in task.stages:
        stage.plan = clip(stage.plan, 2000)


def _ordered_stages(task: LongHorizonTask) -> list[LongHorizonStage]:
    return sorted(
        task.stages,
        key=lambda stage: (stage.due_at or "9999", stage.offset_days, stage.id),
    )


def _stage_progress(
    task: LongHorizonTask, current: LongHorizonStage
) -> list[str]:
    lines: list[str] = []
    for stage in _ordered_stages(task):
        status = _STATUS_LABEL.get(stage.status, stage.status)
        marker = " ←当前" if stage.id == current.id else ""
        conclusion = (
            f"：{clip(stage.conclusion, 80)}" if stage.conclusion.strip() else ""
        )
        lines.append(f"- {stage.title}（{status}）{conclusion}{marker}")
    return lines


def previous_finished_stage(
    task: LongHorizonTask, current: LongHorizonStage
) -> LongHorizonStage | None:
    ordered = _ordered_stages(task)
    current_index = next(
        (index for index, stage in enumerate(ordered) if stage.id == current.id),
        len(ordered),
    )
    return next(
        (
            stage
            for stage in reversed(ordered[:current_index])
            if stage.status in ("done", "skipped")
        ),
        None,
    )


def next_open_stage(task: LongHorizonTask) -> LongHorizonStage | None:
    """First unfinished stage in due order (skip done/skipped)."""
    return next(
        (
            stage
            for stage in _ordered_stages(task)
            if stage.status not in ("done", "skipped")
        ),
        None,
    )


def earlier_unfinished_stages(
    task: LongHorizonTask, current: LongHorizonStage
) -> list[LongHorizonStage]:
    ordered = _ordered_stages(task)
    current_index = next(
        (index for index, stage in enumerate(ordered) if stage.id == current.id),
        len(ordered),
    )
    return [
        stage
        for stage in ordered[:current_index]
        if stage.status not in ("done", "skipped")
    ]


def build_kick_query(
    task: LongHorizonTask,
    stage: LongHorizonStage,
    history_snippet: str = "",
) -> str:
    brief = clip(task.brief, 800)
    progress = "\n".join(_stage_progress(task, stage))
    context_parts: list[str] = []
    earlier = earlier_unfinished_stages(task, stage)
    if earlier:
        names = "、".join(item.title for item in earlier)
        context_parts.append(
            f"注意：前置阶段尚未完成（{names}）；"
            "用户选择先做本阶段，请只推进本阶段，不要去 start 前置或其他阶段。"
        )
    previous = previous_finished_stage(task, stage)
    if previous is not None and previous.conclusion.strip():
        context_parts.append(
            f"上一阶段「{previous.title}」结论：{clip(previous.conclusion, 400)}"
        )
    elif history_snippet.strip():
        context_parts.append(
            "执行会话近况（上一阶段未写结论，从对话摘录）：\n"
            + clip(history_snippet, 800)
        )
    context = "\n".join(context_parts)
    return STAGE_START_PROMPT.format(
        title=task.title,
        stage_title=stage.title,
        plan=clip(stage.plan, 2000),
        brief=brief,
        progress=progress,
        context=context,
    )


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def _events_path(workspace: str | Path) -> Path:
    raw = str(workspace or "").strip()
    if not raw:
        raise ValueError("workspace_required")
    return Path(raw) / "long_horizon_events.jsonl"


def append_event(
    workspace: str | Path,
    *,
    task_id: str,
    stage_id: str = "",
    action: str,
    extra: dict[str, Any] | None = None,
) -> None:
    path = _events_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        **(extra or {}),
        "at": touch_iso(),
        "task_id": task_id,
        "stage_id": stage_id,
        "action": action,
    }
    try:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.debug("[long_horizon] append event failed: %s", exc)


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
_STORE_NAME = "long_horizon_tasks.json"


def _workspace_path(workspace: str | Path) -> Path:
    raw = str(workspace or "").strip()
    if not raw:
        raise ValueError("workspace_required")
    return Path(raw)


def _store_path(workspace: str | Path) -> Path:
    return _workspace_path(workspace) / _STORE_NAME


def load_long_horizon_tasks(workspace: str | Path) -> list[LongHorizonTask]:
    path = _store_path(workspace)
    if not path.exists() or path.stat().st_size == 0:
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("[long_horizon] load failed: %s", exc)
        return []
    rows = payload.get("tasks") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    tasks: list[LongHorizonTask] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            logger.warning(
                "[long_horizon] skipping corrupt record at index %d", index
            )
            continue
        try:
            tasks.append(LongHorizonTask.from_dict(row))
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[long_horizon] skipping corrupt record at index %d: %s",
                index,
                exc,
            )
    return tasks


def _save_long_horizon_tasks(
    workspace: str | Path, tasks: list[LongHorizonTask]
) -> None:
    path = _store_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tasks": [task.to_dict() for task in tasks],
        "updated_at": touch_iso(),
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temp_path = Path(stream.name)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def get_long_horizon_task(
    workspace: str | Path, task_id: str
) -> LongHorizonTask | None:
    normalized = str(task_id or "").strip()
    if not normalized:
        return None
    return next(
        (
            task
            for task in load_long_horizon_tasks(workspace)
            if task.id == normalized
        ),
        None,
    )


def upsert_long_horizon_task(
    workspace: str | Path, task: LongHorizonTask
) -> LongHorizonTask:
    with _LOCK:
        tasks = load_long_horizon_tasks(workspace)
        task.updated_at = touch_iso()
        for index, current in enumerate(tasks):
            if current.id == task.id:
                tasks[index] = task
                break
        else:
            if not task.created_at:
                task.created_at = task.updated_at
            tasks.append(task)
        _save_long_horizon_tasks(workspace, tasks)
    return task


def delete_long_horizon_task(workspace: str | Path, task_id: str) -> bool:
    with _LOCK:
        normalized = str(task_id or "").strip()
        tasks = load_long_horizon_tasks(workspace)
        kept = [task for task in tasks if task.id != normalized]
        if len(kept) == len(tasks):
            return False
        _save_long_horizon_tasks(workspace, kept)
        return True


def list_inbox(workspace: str | Path) -> list[dict]:
    """Stages that are due or snooze-expired for active tasks."""
    now = datetime.now(timezone.utc)
    inbox: list[dict] = []
    for task in load_long_horizon_tasks(workspace):
        if task.status != "active":
            continue
        for stage in task.stages:
            if stage.status == "due":
                inbox.append({"task": task.to_dict(), "stage": stage.to_dict()})
            elif stage.status == "snoozed" and stage.snooze_until:
                try:
                    until = datetime.fromisoformat(
                        stage.snooze_until.replace("Z", "+00:00")
                    )
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=timezone.utc)
                    if until <= now:
                        inbox.append(
                            {"task": task.to_dict(), "stage": stage.to_dict()}
                        )
                except Exception:
                    inbox.append({"task": task.to_dict(), "stage": stage.to_dict()})
    return inbox
