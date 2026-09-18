# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Action dispatcher for long-horizon tasks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from openjiuwen.core.foundation.tool.tool import tool

from jiuwenswarm.agents.harness.common.long_horizon.core import (
    ScheduleError,
    append_event,
    build_kick_query,
    clip,
    delete_long_horizon_task,
    draft_long_horizon_task,
    fill_stage_due_dates,
    get_long_horizon_task,
    list_inbox,
    load_long_horizon_tasks,
    next_open_stage,
    previous_finished_stage,
    require_brief,
    require_stage_plans,
    rewrite_task_stages,
    sanitize_task_title,
    shift_due_at,
    stages_from_specs,
    upsert_long_horizon_task,
)
from jiuwenswarm.agents.harness.common.long_horizon.models import LongHorizonTask
from jiuwenswarm.agents.harness.common.long_horizon.runtime import (
    CronBackend,
    cancel_long_horizon_crons,
    compile_long_horizon_crons,
    deliver_stage_reminder,
    ensure_exec_session_id,
    schedule_stage_jobs_transactional,
)

logger = logging.getLogger(__name__)


@dataclass
class LongHorizonToolParams:
    action: str
    title: str | None = None
    kind: str | None = None
    month: int | None = None
    day: int | None = None
    recurrence: str | None = None
    timezone: str | None = None
    task_id: str | None = None
    stage_id: str | None = None
    stage_action: str | None = None
    snooze_hours: int | None = None
    draft_json: dict[str, Any] | None = None
    stages: list | None = None
    hour: int | None = None
    minute: int | None = None
    brief: str | None = None
    conclusion: str | None = None
    now: str | None = None
    source: str | None = None


class LongHorizonActions:
    def __init__(
        self,
        workspace: str | Any,
        *,
        cron: CronBackend | None = None,
        service_id: str = "default",
        agent_id: str = "default",
    ) -> None:
        self.workspace = str(workspace or "").strip()
        self.cron = cron
        self.service_id = service_id
        self.agent_id = agent_id

    async def handle(
        self, params: LongHorizonToolParams | dict[str, Any]
    ) -> dict[str, Any]:
        normalized = normalize_long_horizon_params(params)
        action = str(normalized.action or "").strip().lower()
        try:
            if action == "draft":
                return await self._action_draft(normalized)
            if action == "confirm":
                return await self._action_confirm(normalized)
            if action == "update":
                return await self._action_update(normalized)
            if action == "list":
                items = load_long_horizon_tasks(self.workspace)
                return {
                    "success": True,
                    "tasks": [item.to_dict() for item in items],
                    "count": len(items),
                }
            if action == "inbox":
                return {"success": True, "inbox": list_inbox(self.workspace)}
            if action == "stage_action":
                return await self._action_stage(normalized)
            if action == "delete":
                return await self._action_delete(normalized)
            return {
                "success": False,
                "error": (
                    "Unknown action. Valid: draft, confirm, update, list, "
                    "inbox, delete, stage_action"
                ),
            }
        except ScheduleError as exc:
            return {"success": False, "error": str(exc)}
        except ValueError as exc:
            code = str(exc)
            if code == "brief_required":
                return {
                    "success": False,
                    "error": "brief_required",
                    "message": "confirm 时必须提供 brief（2-6 句任务简报）",
                }
            if code == "stage_plan_required":
                return {
                    "success": False,
                    "error": "stage_plan_required",
                    "message": (
                        "每个阶段必须提供 plan（自然语言：本阶段做什么、怎么做）"
                    ),
                }
            return {"success": False, "error": code}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @staticmethod
    def _parse_now(params: LongHorizonToolParams) -> datetime | None:
        raw = str(params.now or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    async def _action_draft(self, params: LongHorizonToolParams) -> dict[str, Any]:
        title = sanitize_task_title(str(params.title or "").strip())
        if not title:
            return {"success": False, "error": "title is required for draft"}
        month, day = _resolved_anchor(params)
        task = draft_long_horizon_task(
            title=title,
            kind=str(params.kind or "generic"),
            month=month,
            day=day,
            recurrence=str(params.recurrence or "once"),
            timezone=str(params.timezone or "Asia/Shanghai"),
            stages=_stage_specs_from_params(params),
            hour=int(params.hour) if params.hour is not None else 9,
            minute=int(params.minute) if params.minute is not None else 0,
            now=self._parse_now(params),
        )
        incoming_brief = str(params.brief or "").strip()
        if incoming_brief:
            task.brief = clip(incoming_brief, 800)
        try:
            require_stage_plans(task)
        except ValueError:
            return {
                "success": False,
                "error": "stage_plan_required",
                "message": (
                    "每个阶段必须提供 plan（自然语言写清本阶段做什么、怎么做）；"
                    "缺 plan 不要 draft。"
                ),
                "missing_stages": [
                    str(stage.title or stage.id)
                    for stage in task.stages
                    if not str(stage.plan or "").strip()
                ],
            }
        upsert_long_horizon_task(self.workspace, task)
        append_event(self.workspace, task_id=task.id, action="draft")
        return {
            "success": True,
            "draft": _draft_preview_dict(task),
            "task": task.to_dict(),
            "wait_for_user": True,
            "message": (
                "草稿已生成。下一步用 ask_user 向用户确认是否创建这个长程任务，"
                "不要立即 confirm。"
                "ask_user 选项只给「确认创建」一类明确同意项即可；"
                "不要再造「需要修改」「自定义」「Other」——"
                "系统会自动追加自定义输入，用户要改内容时选它并填写即可。"
            ),
        }

    async def _action_confirm(
        self, params: LongHorizonToolParams
    ) -> dict[str, Any]:
        raw = params.draft_json
        if isinstance(raw, dict) and raw.get("id"):
            task = LongHorizonTask.from_dict(raw)
        elif params.task_id:
            existing = get_long_horizon_task(self.workspace, str(params.task_id))
            if existing is None:
                return {"success": False, "error": "task_not_found"}
            task = existing
        elif params.title:
            drafted = await self._action_draft(params)
            if not drafted.get("success"):
                return drafted
            task = LongHorizonTask.from_dict(drafted["task"])
        else:
            return {
                "success": False,
                "error": "provide draft_json, task_id, or title",
            }

        if task.status == "active" and task.exec_session_id:
            # Idempotent re-confirm: refresh cron jobs only.
            try:
                require_brief(task, str(params.brief or "").strip() or task.brief)
                require_stage_plans(task)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            task = await compile_long_horizon_crons(
                task,
                cron=self.cron,
                targets="web",
                service_id=self.service_id,
                agent_id=self.agent_id,
            )
            upsert_long_horizon_task(self.workspace, task)
            return {
                "success": True,
                "stop_after_confirm": True,
                "message": f"长程任务「{task.title}」已确认（幂等刷新调度）。",
                "task": task.to_dict(),
            }

        if params.title:
            task.title = sanitize_task_title(str(params.title).strip())
        stage_specs = _stage_specs_from_params(params)
        hour = int(params.hour) if params.hour is not None else 9
        minute = int(params.minute) if params.minute is not None else 0
        now = self._parse_now(params)
        if params.timezone:
            task.anchor.timezone = str(params.timezone)
        if stage_specs is not None:
            anchor_month, anchor_day = _infer_anchor_month_day(stage_specs)
            task.anchor.month = anchor_month
            task.anchor.day = anchor_day
            task.stages = stages_from_specs(
                stage_specs, task=task, hour=hour, minute=minute, now=now
            )
            fill_stage_due_dates(task, hour=hour, minute=minute, now=now)

        incoming_brief = str(params.brief or "").strip()
        if not incoming_brief and isinstance(params.draft_json, dict):
            incoming_brief = str(params.draft_json.get("brief") or "").strip()
        try:
            task.brief = require_brief(task, incoming_brief)
        except ValueError:
            return {
                "success": False,
                "error": "brief_required",
                "message": (
                    "confirm 时必须提供 brief（2-6 句：目标、约束、交付物、关键背景）"
                ),
            }
        try:
            require_stage_plans(task)
        except ValueError:
            return {
                "success": False,
                "error": "stage_plan_required",
                "message": (
                    "confirm 前每个阶段都要有 plan（自然语言：做什么、怎么做）。"
                ),
                "missing_stages": [
                    str(stage.title or stage.id)
                    for stage in task.stages
                    if not str(stage.plan or "").strip()
                ],
            }

        ensure_exec_session_id(task)
        # Keep draft on disk until jobs succeed.
        try:
            await schedule_stage_jobs_transactional(
                task,
                cron=self.cron,
                targets="web",
                service_id=self.service_id,
                agent_id=self.agent_id,
            )
        except Exception as exc:
            for stage in task.stages:
                stage.cron_job_id = ""
            # Ensure persisted status remains draft.
            stored = get_long_horizon_task(self.workspace, task.id)
            if stored is not None:
                stored.status = "draft"
                stored.exec_session_id = stored.exec_session_id or ""
                for stage in stored.stages:
                    stage.cron_job_id = ""
                upsert_long_horizon_task(self.workspace, stored)
            return {
                "success": False,
                "error": "cron_schedule_failed",
                "message": str(exc),
            }

        task.status = "active"
        upsert_long_horizon_task(self.workspace, task)
        append_event(self.workspace, task_id=task.id, action="created")
        delivered = await self._deliver_overdue_stages_after_confirm(task)
        if delivered:
            refreshed = get_long_horizon_task(self.workspace, task.id)
            if refreshed is not None:
                task = refreshed
        cycle = (
            "本轮一次，不会自动逐年重复"
            if task.recurrence != "yearly"
            else "按年重复登记（今年节点已挂上）"
        )
        return {
            "success": True,
            "stop_after_confirm": True,
            "overdue_stages_delivered": delivered,
            "message": (
                f"已创建长程任务「{task.title}」（{cycle}），"
                f"阶段提醒将推送到专用会话 {task.exec_session_id}（不打断主会话）。"
                "本回合结束，不要写交付物或 stage_action；等用户点「现在做」"
                "或到点提醒后再开工。"
            ),
            "task": task.to_dict(),
        }

    async def _action_update(
        self, params: LongHorizonToolParams
    ) -> dict[str, Any]:
        """Rewrite the full stages list for an active task (Plan A).

        Preserves ``task.id`` and ``exec_session_id`` so the dedicated session
        ``longhorizon_<task_id>`` stays the same before/after.
        """
        task, err = _resolve_task(self.workspace, params)
        if err is not None:
            return err
        if task is None:
            return {"success": False, "error": "task_not_found"}
        if task.status not in ("active", "muted_year"):
            return {
                "success": False,
                "error": "task_not_active",
                "message": "update 仅用于已 confirm 的任务；草稿请直接改 stages 再 confirm。",
            }
        stage_specs = _stage_specs_from_params(params)
        if stage_specs is None:
            return {
                "success": False,
                "error": "stages_required",
                "message": (
                    "update 须提交完整 stages 列表（与 draft 相同："
                    "每项 title / due_at / plan；中间插入就写在对应位置）。"
                ),
            }

        task_id = task.id
        exec_session_id = task.exec_session_id
        if not exec_session_id:
            exec_session_id = ensure_exec_session_id(task)

        if params.title:
            task.title = sanitize_task_title(str(params.title).strip())
        incoming_brief = str(params.brief or "").strip()
        if incoming_brief:
            task.brief = clip(incoming_brief, 800)
        if params.timezone:
            task.anchor.timezone = str(params.timezone)

        hour = int(params.hour) if params.hour is not None else 9
        minute = int(params.minute) if params.minute is not None else 0
        now = self._parse_now(params)
        anchor_month, anchor_day = _infer_anchor_month_day(stage_specs)
        task.anchor.month = anchor_month
        task.anchor.day = anchor_day

        try:
            rewrite_task_stages(
                task, stage_specs, hour=hour, minute=minute, now=now
            )
            require_stage_plans(task)
        except ValueError as exc:
            code = str(exc)
            if code == "stage_plan_required":
                return {
                    "success": False,
                    "error": "stage_plan_required",
                    "message": "update 时每个阶段都必须有 plan。",
                    "missing_stages": [
                        str(stage.title or stage.id)
                        for stage in task.stages
                        if not str(stage.plan or "").strip()
                    ],
                }
            return {"success": False, "error": code}

        # Hard pin identity — never allocate a new session on rewrite.
        task.id = task_id
        task.exec_session_id = exec_session_id
        if task.status == "muted_year":
            task.status = "active"

        task = await compile_long_horizon_crons(
            task,
            cron=self.cron,
            targets="web",
            service_id=self.service_id,
            agent_id=self.agent_id,
        )
        task.id = task_id
        task.exec_session_id = exec_session_id
        upsert_long_horizon_task(self.workspace, task)
        append_event(self.workspace, task_id=task.id, action="updated")
        return {
            "success": True,
            "message": (
                f"已更新长程任务「{task.title}」阶段计划，"
                f"执行会话仍为 {task.exec_session_id}。"
            ),
            "task": task.to_dict(),
            "exec_session_id": task.exec_session_id,
        }

    async def _deliver_overdue_stages_after_confirm(
        self, task: LongHorizonTask
    ) -> list[str]:
        tz = ZoneInfo(task.anchor.timezone or "Asia/Shanghai")
        now = datetime.now(tz)
        delivered: list[str] = []
        for stage in task.stages:
            if stage.status not in ("pending", "snoozed"):
                continue
            if not stage.due_at:
                continue
            try:
                due = datetime.fromisoformat(stage.due_at.replace("Z", "+00:00"))
                if due.tzinfo is None:
                    due = due.replace(tzinfo=tz)
                else:
                    due = due.astimezone(tz)
            except Exception as exc:
                logger.debug(
                    "[long_horizon] skip overdue parse task=%s stage=%s: %s",
                    task.id,
                    stage.id,
                    exc,
                )
                continue
            if due <= now:
                result = await deliver_stage_reminder(
                    self.workspace, task.id, stage.id
                )
                if result.get("success"):
                    delivered.append(stage.id)
                break
        return delivered

    async def _action_stage(
        self, params: LongHorizonToolParams
    ) -> dict[str, Any]:
        task_id = str(params.task_id or "").strip()
        stage_id = str(params.stage_id or "").strip()
        stage_action = str(params.stage_action or "").strip().lower()
        if not stage_action and isinstance(params.draft_json, dict):
            stage_action = str(
                params.draft_json.get("stage_action")
                or params.draft_json.get("action")
                or ""
            ).strip().lower()

        task = get_long_horizon_task(self.workspace, task_id)
        if task is None:
            return {"success": False, "error": "task_not_found"}
        stage = next((item for item in task.stages if item.id == stage_id), None)

        if stage_action == "mute_year":
            task.status = "muted_year"
            await cancel_long_horizon_crons(
                task,
                cron=self.cron,
                service_id=self.service_id,
                agent_id=self.agent_id,
            )
            append_event(self.workspace, task_id=task.id, action="mute_year")
            upsert_long_horizon_task(self.workspace, task)
            return {
                "success": True,
                "message": "已静音本周期",
                "task": task.to_dict(),
            }
        if stage is None:
            return {"success": False, "error": "stage_not_found"}

        if stage_action == "done":
            stage.status = "done"
            _apply_stage_conclusion(params, stage)
            append_event(
                self.workspace, task_id=task.id, stage_id=stage.id, action="done"
            )
        elif stage_action == "skip":
            stage.status = "skipped"
            _apply_stage_conclusion(params, stage)
            append_event(
                self.workspace, task_id=task.id, stage_id=stage.id, action="skip"
            )
        elif stage_action == "snooze":
            hours = int(params.snooze_hours or 2)
            tz_name = task.anchor.timezone or "Asia/Shanghai"
            until = shift_due_at(timezone=tz_name, hours=hours)
            stage.status = "snoozed"
            stage.snooze_until = until
            stage.due_at = until
            # One-shot job was deleted on fire; clear id so compile recreates.
            stage.cron_job_id = ""
            append_event(
                self.workspace,
                task_id=task.id,
                stage_id=stage.id,
                action="snooze",
                extra={"hours": hours, "due_at": until},
            )
        elif stage_action == "defer":
            tz_name = task.anchor.timezone or "Asia/Shanghai"
            base = None
            if stage.due_at:
                try:
                    base = datetime.fromisoformat(
                        stage.due_at.replace("Z", "+00:00")
                    )
                except Exception as exc:
                    logger.debug(
                        "[long_horizon] defer base due parse skipped: %s", exc
                    )
                    base = None
            new_due = shift_due_at(timezone=tz_name, days=7, base=base)
            stage.due_at = new_due
            stage.status = "pending"
            stage.snooze_until = ""
            stage.cron_job_id = ""
            append_event(
                self.workspace, task_id=task.id, stage_id=stage.id, action="defer"
            )
        elif stage_action == "start":
            if stage.status in ("done", "skipped"):
                return {
                    "success": False,
                    "error": f"stage_already_{stage.status}",
                }
            # User (Toast / Cron「立即执行」): start whichever stage they clicked.
            # Agent tool calls: only the next unfinished stage (no jumping ahead).
            source = str(params.source or "").strip().lower()
            user_initiated = source in {"user", "web", "cron", "toast"}
            if (
                not user_initiated
                and stage.status in ("pending", "snoozed", "due")
            ):
                nxt = next_open_stage(task)
                if nxt is None or nxt.id != stage.id:
                    return {
                        "success": False,
                        "error": "stage_not_due",
                        "message": (
                            "执行会话内不要 start 后续阶段；"
                            "只推进当前阶段，完成后用 done 收口。"
                            "用户若要提前做某阶段，请通过 Toast/Cron「立即执行」。"
                        ),
                    }
            stage.status = "in_progress"
            append_event(
                self.workspace, task_id=task.id, stage_id=stage.id, action="start"
            )
            ensure_exec_session_id(task)
            task = await compile_long_horizon_crons(
                task,
                cron=self.cron,
                service_id=self.service_id,
                agent_id=self.agent_id,
            )
            upsert_long_horizon_task(self.workspace, task)
            from jiuwenswarm.agents.harness.common.long_horizon.runtime import (
                _ensure_session_metadata,
            )

            await _ensure_session_metadata(task, channel_id="web")
            history_snippet = ""
            prev = previous_finished_stage(task, stage)
            if prev is not None and not str(prev.conclusion or "").strip():
                history_snippet = ""
            return {
                "success": True,
                "message": f"已打开执行会话 {task.exec_session_id}",
                "exec_session_id": task.exec_session_id,
                "kick_query": build_kick_query(
                    task, stage, history_snippet=history_snippet
                ),
                "task": task.to_dict(),
            }
        else:
            return {
                "success": False,
                "error": (
                    "stage_action required: done|snooze|defer|start|mute_year|skip"
                ),
            }

        task = await compile_long_horizon_crons(
            task,
            cron=self.cron,
            service_id=self.service_id,
            agent_id=self.agent_id,
        )
        upsert_long_horizon_task(self.workspace, task)
        return {
            "success": True,
            "task": task.to_dict(),
            "stage_action": stage_action,
        }

    async def _action_delete(
        self, params: LongHorizonToolParams
    ) -> dict[str, Any]:
        task, err = _resolve_task(self.workspace, params)
        if err is not None:
            return err
        if task is None:
            return {"success": False, "error": "task_not_found"}
        title = task.title
        task_id = task.id
        snapshot = task.to_dict()
        await cancel_long_horizon_crons(
            task,
            cron=self.cron,
            service_id=self.service_id,
            agent_id=self.agent_id,
        )
        delete_long_horizon_task(self.workspace, task_id)
        append_event(self.workspace, task_id=task_id, action="deleted")
        return {
            "success": True,
            "deleted": True,
            "message": f"已删除长程任务「{title}」，对应阶段提醒已取消。",
            "task": snapshot,
        }


def _draft_preview_dict(task: LongHorizonTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "kind": task.kind,
        "anchor": task.anchor.to_dict(),
        "recurrence": task.recurrence,
        "brief": task.brief,
        "stages": [
            {
                "id": stage.id,
                "offset_days": stage.offset_days,
                "title": stage.title,
                "hint": stage.hint,
                "plan": stage.plan,
                "due_at": stage.due_at,
            }
            for stage in task.stages
        ],
        "note": (
            "下一步用 ask_user 向用户确认是否创建；不要在同一回合 confirm。"
            "ask_user 选项只给「确认创建」；不要额外造「需要修改/自定义/Other」"
            "（系统已自动提供自定义输入）。"
        ),
    }


def _stage_specs_from_params(params: LongHorizonToolParams) -> Any:
    if params.stages:
        return params.stages
    raw = params.draft_json
    if isinstance(raw, dict) and raw.get("stages"):
        return raw.get("stages")
    return None


def _clamp_day(month: int, day: int) -> int:
    month = max(1, min(int(month), 12))
    return max(1, min(int(day), 28 if month == 2 else 31))


def _parse_stage_due_hint(item: dict[str, Any]) -> datetime | None:
    """Best-effort parse of stage due for anchor picking (year-aware)."""
    tz = ZoneInfo("Asia/Shanghai")
    due_at = str(item.get("due_at") or "").strip()
    if due_at:
        try:
            text = due_at.replace("Z", "+00:00")
            if "T" not in text and " " in text and text.count("-") >= 2:
                text = text.replace(" ", "T", 1)
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            return parsed
        except ValueError:
            pass
    date_raw = str(item.get("date") or item.get("due") or "").strip()
    if date_raw:
        try:
            date_part = date_raw.replace("Z", "+00:00").split("T", 1)[0].split(" ", 1)[0]
            year_s, month_s, day_s = date_part.split("-", 2)
            return datetime(
                int(year_s), int(month_s), int(day_s), 9, 0, tzinfo=tz
            )
        except (TypeError, ValueError):
            return None
    sm = item.get("month")
    sd = item.get("day")
    if sm is None or sd is None:
        return None
    if str(sm).strip() == "" or str(sd).strip() == "":
        return None
    try:
        month = max(1, min(int(sm), 12))
        day = _clamp_day(month, int(sd))
        # Legacy month/day only: use a synthetic year so ordering still works
        # within a single calendar cycle (year is not meaningful here).
        return datetime(2000, month, day, 9, 0, tzinfo=tz)
    except (TypeError, ValueError):
        return None


def _infer_anchor_month_day(specs: Any | None) -> tuple[int, int]:
    """Anchor = latest stage due among specs (year-aware when due_at is set)."""
    from jiuwenswarm.agents.harness.common.long_horizon.core import (
        coerce_stage_specs,
    )

    best: datetime | None = None
    try:
        rows = coerce_stage_specs(specs)
    except ScheduleError:
        rows = []
    for item in rows:
        parsed = _parse_stage_due_hint(item)
        if parsed is None:
            continue
        if best is None or parsed > best:
            best = parsed
    if best is not None:
        return best.month, best.day
    return 8, 1


def normalize_long_horizon_params(
    params: LongHorizonToolParams | dict[str, Any],
) -> LongHorizonToolParams:
    raw: dict[str, Any]
    if isinstance(params, LongHorizonToolParams):
        raw = {
            key: getattr(params, key)
            for key in LongHorizonToolParams.__dataclass_fields__
            if getattr(params, key) is not None
        }
    elif isinstance(params, dict):
        raw = dict(params)
    else:
        raw = {}

    # Compatibility alias from Spirit/recommendation naming.
    if "task_id" not in raw and raw.get("commitment_id"):
        raw["task_id"] = raw.get("commitment_id")

    inner = raw.get("params")
    if isinstance(inner, dict):
        merged = dict(inner)
        for key, value in raw.items():
            if key != "params" and key not in merged:
                merged[key] = value
        raw = merged

    # Drop legacy top-level month/day — anchor is always inferred from stages.
    raw.pop("month", None)
    raw.pop("day", None)

    action = str(raw.get("action") or "").strip().lower()
    month: int | None = None
    day: int | None = None
    if action != "confirm":
        month, day = _infer_anchor_month_day(_stage_specs_from_params_from_raw(raw))

    kwargs: dict[str, Any] = {}
    for key in LongHorizonToolParams.__dataclass_fields__:
        if key in raw and raw[key] is not None:
            kwargs[key] = raw[key]
    if month is not None:
        kwargs["month"] = month
    if day is not None:
        kwargs["day"] = day
    return LongHorizonToolParams(**kwargs)


def _stage_specs_from_params_from_raw(raw: dict[str, Any]) -> Any:
    stages = raw.get("stages")
    if stages is None and isinstance(raw.get("draft_json"), dict):
        stages = raw["draft_json"].get("stages")
    return stages


def _resolved_anchor(params: LongHorizonToolParams) -> tuple[int, int]:
    return _infer_anchor_month_day(_stage_specs_from_params(params))


def _resolve_task(workspace: str, params: LongHorizonToolParams):
    task_id = str(params.task_id or "").strip()
    if task_id:
        found = get_long_horizon_task(workspace, task_id)
        if found is not None:
            return found, None
    title = str(params.title or "").strip()
    if not title:
        return None, {"success": False, "error": "task_not_found"}
    matches = [
        task for task in load_long_horizon_tasks(workspace) if task.title == title
    ]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, {
            "success": False,
            "error": "multiple_matches",
            "task_ids": [task.id for task in matches],
        }
    return None, {"success": False, "error": "task_not_found"}


def _apply_stage_conclusion(params: LongHorizonToolParams, stage) -> None:
    text = str(params.conclusion or "").strip()
    if not text and isinstance(params.draft_json, dict):
        text = str(params.draft_json.get("conclusion") or "").strip()
    if text:
        stage.conclusion = clip(text, 400)


def _default_long_horizon_workspace() -> str:
    from jiuwenswarm.common.utils import get_agent_workspace_dir

    return str(get_agent_workspace_dir())


_DESCRIPTION_CN = (
    "管理跨日期多阶段长程任务。用户给出未来截止节点时使用。"
    "动作：draft / confirm / update / list / inbox / delete / stage_action。"
    "title 用简短主题名（如「智能体高校合作筹备」），"
    "不要加「（五阶段推进）」这类阶段数后缀。"
    "draft 传 stages：每项 title、due_at、plan；"
    "due_at 固定格式 YYYY-MM-DD HH:MM（含年，时区 Asia/Shanghai）；"
    "未点名钟点用 09:00，例如 2026-09-25 14:00 或 2026-10-08 09:00。"
    "draft 后 ask_user 确认（选项只需「确认创建」），同回合不要 confirm；"
    "confirm 须带 brief，成功后本回合停止。"
    "update：已创建任务改计划时交完整 stages（可中间插阶段）；"
    "task_id / exec_session_id 不变。"
    "stage_action：执行会话内不要 start 其他阶段；"
    "用户点 Toast/Cron「立即执行」可开任意未完成阶段；本阶段完成用 done。"
)

_DESCRIPTION_EN = (
    "Manage multi-stage long-horizon tasks when the user names future checkpoints. "
    "Actions: draft, confirm, update, list, inbox, delete, stage_action. "
    "title is a short topic name (e.g. campus collab prep); "
    "do not append meta suffixes like '(5-stage plan)'. "
    "On draft, pass stages with title, due_at, plan; "
    "due_at format is YYYY-MM-DD HH:MM (include year, Asia/Shanghai); "
    "use 09:00 when no clock was named, e.g. 2026-09-25 14:00. "
    "After draft, ask_user to confirm (approve only); do not confirm same turn; "
    "confirm needs brief, then stop. "
    "update: rewrite the full stages list for an active task (mid-list insert ok); "
    "task_id and exec_session_id stay the same. "
    "stage_action: do not start other stages from the exec session; "
    "user Toast/Cron Run-now may open any unfinished stage; finish with done."
)


def _normalize_tool_language(language: str | None) -> str:
    text = str(language or "cn").strip().lower()
    return "en" if text in {"en", "english"} else "cn"


def _tool_description(language: str | None) -> str:
    if _normalize_tool_language(language) == "en":
        return _DESCRIPTION_EN
    return _DESCRIPTION_CN


def _stage_item_schema(*, language: str) -> dict[str, Any]:
    if language == "en":
        return {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Existing stage id when updating (optional)",
                },
                "title": {"type": "string", "description": "Stage title"},
                "due_at": {
                    "type": "string",
                    "description": (
                        'Reminder time, fixed format "YYYY-MM-DD HH:MM" '
                        "(include year; Asia/Shanghai). Use 09:00 if no clock "
                        'was named, e.g. "2026-09-25 14:00" or "2026-10-08 09:00"'
                    ),
                },
                "plan": {
                    "type": "string",
                    "description": "What to do in this stage",
                },
            },
            "required": ["title", "due_at", "plan"],
        }
    return {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "update 时保留已有阶段 id（可选）",
            },
            "title": {"type": "string", "description": "阶段标题"},
            "due_at": {
                "type": "string",
                "description": (
                    '提醒时间，固定格式 "YYYY-MM-DD HH:MM"（必须含年，'
                    "时区 Asia/Shanghai）。用户未点名钟点则用 09:00，"
                    '例如 "2026-09-25 14:00" 或 "2026-10-08 09:00"'
                ),
            },
            "plan": {"type": "string", "description": "本阶段做什么"},
        },
        "required": ["title", "due_at", "plan"],
    }


def _tool_input_params(language: str | None) -> dict[str, Any]:
    """Agent-facing schema only — no legacy/test-only fields."""
    lang = _normalize_tool_language(language)
    stage_item = _stage_item_schema(language=lang)
    if lang == "en":
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "draft | confirm | update | list | inbox | delete | "
                        "stage_action"
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Short topic title; no '(N-stage ...)' suffix"
                    ),
                },
                "stages": {
                    "type": "array",
                    "description": "Stages for draft or update (full list)",
                    "items": stage_item,
                },
                "task_id": {"type": "string", "description": "Existing task id"},
                "brief": {
                    "type": "string",
                    "description": "Brief required on confirm; optional on update",
                },
                "stage_id": {
                    "type": "string",
                    "description": "Stage id for stage_action",
                },
                "stage_action": {
                    "type": "string",
                    "description": (
                        "start | done | snooze | defer | skip; "
                        "agent: do not start other stages; "
                        "user Run-now may open any unfinished stage"
                    ),
                },
            },
            "required": ["action"],
        }
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "draft | confirm | update | list | inbox | delete | "
                    "stage_action"
                ),
            },
            "title": {
                "type": "string",
                "description": "简短主题名；不要加「（五阶段推进）」一类后缀",
            },
            "stages": {
                "type": "array",
                "description": "draft / update 的完整阶段列表",
                "items": stage_item,
            },
            "task_id": {"type": "string", "description": "已有任务 id"},
            "brief": {
                "type": "string",
                "description": "confirm 必填简报；update 可选",
            },
            "stage_id": {
                "type": "string",
                "description": "stage_action 的阶段 id",
            },
            "stage_action": {
                "type": "string",
                "description": (
                    "start | done | snooze | defer | skip；"
                    "执行会话勿 start 其他阶段；"
                    "用户立即执行可开任意未完成阶段"
                ),
            },
        },
        "required": ["action"],
    }


@tool(
    name="long_horizon_task",
    description=_DESCRIPTION_CN,
    # Module-level singleton; shared registration under bare id.
    stateless=True,
)
async def long_horizon_task(params: LongHorizonToolParams) -> dict[str, Any]:
    """Dispatch long-horizon task actions for ordinary Agent mode."""
    actions = LongHorizonActions(_default_long_horizon_workspace())
    return await actions.handle(params)


def get_decorated_tools(*, language: str = "cn") -> list:
    """Return ``long_horizon_task`` with language-specific description and schema."""
    from openjiuwen.core.foundation.tool import LocalFunction, ToolCard

    description = _tool_description(language)
    input_params = _tool_input_params(language)

    async def _invoke(**kwargs: Any) -> dict[str, Any]:
        actions = LongHorizonActions(_default_long_horizon_workspace())
        return await actions.handle(kwargs or {})

    card = ToolCard(
        id="long_horizon_task",
        name="long_horizon_task",
        description=description,
        input_params=input_params,
    )
    return [LocalFunction(card=card, func=_invoke)]
