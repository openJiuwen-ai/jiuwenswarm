# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Structured run-log status helpers for scheduled auto-harness tasks."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

META_EVOLVE_STAGE_ORDER = [
    "assess",
    "plan",
    "implement",
    "verify",
    "commit",
    "publish",
    "learnings",
]

STAGE_DISPLAY_NAMES = {
    "assess": "评估",
    "plan": "规划",
    "implement": "实现",
    "verify": "验证",
    "commit": "提交",
    "publish": "发布 PR",
    "learnings": "经验总结",
    "build_verify": "构建验证",
    "activate": "激活",
}

SkippedStageInferer = Callable[[str], tuple[str, ...]]
ProgressEnricher = Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]]


def infer_skipped_stages_from_message(
    content: str,
    inferers: list[SkippedStageInferer] | None = None,
) -> tuple[str, ...]:
    """Infer skipped stages with optional scenario-specific extensions."""
    skipped: list[str] = []
    for inferer in inferers or []:
        for stage in inferer(content):
            if stage not in skipped:
                skipped.append(stage)
    return tuple(skipped)


def classify_failure(error: str, last_message: str = "") -> str:
    """Return a stable failure code suitable for compact UI display."""
    text = f"{error}\n{last_message}".lower()
    if (
        "no allowed files" in text
        or "no changes" in text
        or "did not create a new commit" in text
    ):
        return "no_effective_diff"
    if "git branch push failed" in text or "push failed" in text:
        return "push_rejected"
    verify_failed_keywords = ["lint", "type-check", "ci", "verify"]
    if any(k in text for k in verify_failed_keywords):
        return "verify_failed"
    if "file must be read before editing" in text or "tool" in text:
        return "agent_tool_error"
    return "unknown_failure"


def resolve_latest_task_log_path(task: dict[str, Any], runs_dir: Path) -> Path | None:
    """Resolve the latest existing run log for a scheduled task record."""
    task_id = str(task.get("task_id") or "")
    current_execution_id = str(task.get("current_execution_id") or "")
    if task_id and current_execution_id:
        current = runs_dir / task_id / current_execution_id / "log.json"
        if current.exists():
            return current

    history = task.get("execution_history") or []
    sorted_history = sorted(
        history,
        key=lambda r: r.get("completed_at") or r.get("started_at") or "",
        reverse=True,
    )
    for record in sorted_history:
        candidates: list[Path] = []
        log_path_str = str(record.get("log_path") or "")
        if log_path_str:
            candidates.append(Path(log_path_str))
        execution_id = str(record.get("execution_id") or "")
        if task_id and execution_id:
            candidates.append(runs_dir / task_id / execution_id / "log.json")
        for candidate in candidates:
            if candidate.exists():
                return candidate
    return None


def format_progress_summary(progress: dict[str, Any]) -> str:
    failed_stage = progress.get("failed_stage")
    current_stage = progress.get("current_stage")
    completed = progress.get("completed_stages") or []
    total = len(progress.get("stages") or [])
    if failed_stage:
        return f"失败于 {STAGE_DISPLAY_NAMES.get(failed_stage, failed_stage)}"
    if current_stage:
        current_stage_name = STAGE_DISPLAY_NAMES.get(current_stage, current_stage)
        return f"{len(completed)}/{total} 已完成，正在 {current_stage_name}"
    if total and len(completed) >= total:
        return f"{len(completed)}/{total} 已完成"
    if total:
        return f"{len(completed)}/{total} 已完成"
    return "暂无阶段日志"


def summarize_progress_from_logs(
    logs: list[dict[str, Any]],
    *,
    skipped_stage_inferers: list[SkippedStageInferer] | None = None,
    progress_enrichers: list[ProgressEnricher] | None = None,
) -> dict[str, Any]:
    """Summarize stage progress from structured harness logs."""
    pipeline = ""
    stage_order: list[str] = []
    stage_status: dict[str, str] = {}
    stage_messages: dict[str, list[str]] = {}
    last_stage_message = ""
    last_message_stage = ""
    last_event_type = ""
    last_error = ""

    for entry in logs:
        event_type = str(entry.get("event_type") or "")
        last_event_type = event_type or last_event_type
        if event_type == "harness.message":
            if entry.get("pipeline") and entry.get("stages"):
                pipeline = str(entry.get("pipeline") or pipeline)
                stage_order = [
                    str(stage.get("slot") or "")
                    for stage in entry.get("stages") or []
                    if stage.get("slot")
                ]
            stage = str(entry.get("stage") or "")
            content = str(entry.get("content") or "")
            for skipped_stage in infer_skipped_stages_from_message(content, skipped_stage_inferers):
                stage_status.setdefault(skipped_stage, "skipped")
                if skipped_stage not in stage_order:
                    stage_order.append(skipped_stage)
            if stage:
                last_message_stage = stage
                if content:
                    last_stage_message = content
                    stage_messages.setdefault(stage, []).append(content)
        elif event_type == "harness.stage_result" and not entry.get("scope"):
            stage = str(entry.get("stage") or "")
            status = str(entry.get("status") or "")
            if stage:
                stage_status[stage] = status
                if entry.get("error"):
                    last_error = str(entry.get("error") or "")
                if stage not in stage_order:
                    stage_order.append(stage)
                messages = entry.get("messages") or []
                if messages:
                    stage_messages.setdefault(stage, []).extend(str(msg) for msg in messages)
        elif event_type == "harness.session_finished" and entry.get("error"):
            last_error = str(entry.get("error") or "")

    if not stage_order:
        stage_order = list(META_EVOLVE_STAGE_ORDER)
        for stage in stage_status:
            if stage not in stage_order:
                stage_order.append(stage)

    completed = [
        stage for stage in stage_order
        if stage_status.get(stage) in {"success", "skipped"}
    ]
    failed_stage = next(
        (
            stage for stage in stage_order
            if stage_status.get(stage) == "failed"
        ),
        "",
    )
    current_stage = ""
    if not failed_stage:
        if (
            last_message_stage
            and stage_status.get(last_message_stage) not in {"success", "failed", "skipped"}
        ):
            current_stage = last_message_stage
        else:
            current_stage = next(
                (
                    stage for stage in stage_order
                    if stage_status.get(stage) not in {"success", "skipped"}
                ),
                "",
            )

    stages = []
    for stage in stage_order:
        status = stage_status.get(stage)
        if not status:
            status = "running" if stage == current_stage else "pending"
        recent_messages = stage_messages.get(stage) or []
        stages.append({
            "stage": stage,
            "name": STAGE_DISPLAY_NAMES.get(stage, stage),
            "status": status,
            "messages": recent_messages[-3:],
        })

    progress = {
        "pipeline": pipeline,
        "stages": stages,
        "completed_stages": completed,
        "current_stage": current_stage,
        "failed_stage": failed_stage,
        "last_message": last_stage_message,
        "last_event_type": last_event_type,
        "last_error": last_error,
        "failure_code": classify_failure(last_error, last_stage_message) if failed_stage else "",
    }
    for enricher in progress_enrichers or []:
        progress = enricher(progress, logs)
    progress["summary"] = format_progress_summary(progress)
    return progress


def determine_pipeline_status_from_log(
    log_path: Path,
    *,
    skipped_stage_inferers: list[SkippedStageInferer] | None = None,
) -> dict[str, Any]:
    """Parse a JSON Lines log file and determine whether the pipeline succeeded."""
    pipeline_type = ""
    pipeline_stages: list[str] = []
    stage_results: dict[str, str] = {}

    try:
        with log_path.open("r", encoding="utf-8") as lf:
            for line in lf:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    entry.get("event_type") == "harness.message"
                    and entry.get("stages")
                    and entry.get("pipeline")
                ):
                    pipeline_stages = [s.get("slot") for s in entry["stages"]]
                    pipeline_type = entry.get("pipeline", "")
                if entry.get("event_type") == "harness.message":
                    content = str(entry.get("content") or "")
                    for skipped_stage in infer_skipped_stages_from_message(content, skipped_stage_inferers):
                        stage_results.setdefault(skipped_stage, "skipped")
                if entry.get("event_type") == "harness.stage_result" and not entry.get("scope"):
                    slot = entry.get("stage")
                    status = entry.get("status")
                    if slot:
                        stage_results[slot] = status
    except Exception as exc:
        logger.warning("[AutoHarnessRunLogStatus] Failed to read log %s: %s", log_path, exc)
        return {"failed": False, "error": ""}

    if pipeline_type == "extended_evolve_pipeline":
        if "build_verify" not in stage_results:
            return {"failed": True, "error": "Stage 'build_verify' not appeared"}
        if stage_results.get("activate") not in {"success", "skipped"}:
            return {
                "failed": True,
                "error": f"Stage 'activate' {stage_results.get('activate', 'not completed')}",
            }
        return {"failed": False, "error": ""}

    for slot in pipeline_stages:
        result = stage_results.get(slot)
        if result not in {"success", "skipped"}:
            return {
                "failed": True,
                "error": f"Stage '{slot}' {stage_results.get(slot, 'not completed')}",
            }

    return {"failed": False, "error": ""}


def has_terminal_session_event(log_path: Path) -> bool:
    """Return whether a structured run log contains a terminal session event."""
    try:
        with log_path.open("r", encoding="utf-8") as lf:
            for line in lf:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    entry.get("event_type") == "harness.session_finished"
                    and entry.get("is_terminal") is True
                ):
                    return True
    except Exception as exc:
        logger.warning("[AutoHarnessRunLogStatus] Failed to scan terminal event in %s: %s", log_path, exc)
    return False


# 终态任务状态集合 - 终态任务不需要读取日志
TERMINAL_STATUSES = {
    "success", "failed", "cancelled", "pr_created",
    "completed", "completed_without_pr", "skipped", "needs_human"
}

# 关键事件类型 - 只读取这些事件获取进度
KEY_EVENT_TYPES = {"harness.stage_result", "harness.session_finished"}


def read_key_events_reverse(log_path: Path, max_events: int = 20) -> list[dict[str, Any]]:
    """反向读取日志，只获取关键事件（stage_result, session_finished）。

    用于智能状态查询：运行中任务只需读取关键事件，无需读取全部日志。
    """
    events: list[dict[str, Any]] = []

    try:
        with log_path.open("r", encoding="utf-8") as f:
            f.seek(0, 2)  # 文件末尾
            file_size = f.tell()

            chunk_size = 4096
            buffer = ""
            pos = file_size

            while pos > 0 and len(events) < max_events:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size)
                buffer = chunk + buffer

                lines = buffer.split("\n")
                buffer = lines[0]

                for line in reversed(lines[1:]):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("event_type") in KEY_EVENT_TYPES:
                            events.append(entry)
                    except json.JSONDecodeError:
                        continue

    except Exception as exc:
        logger.warning("[AutoHarnessRunLogStatus] Failed to reverse read %s: %s", log_path, exc)

    return events


def summarize_progress_from_key_events(key_events: list[dict[str, Any]]) -> dict[str, Any]:
    """从关键事件（反向读取）构建进度摘要。"""
    stage_status: dict[str, str] = {}
    stage_messages: dict[str, list[str]] = {}
    last_error = ""
    failed_stage = ""
    pipeline = ""

    # 反向读取的事件是倒序的，需要反转
    for entry in reversed(key_events):
        event_type = str(entry.get("event_type") or "")

        if event_type == "harness.stage_result" and not entry.get("scope"):
            stage = str(entry.get("stage") or "")
            status = str(entry.get("status") or "")
            if stage:
                stage_status[stage] = status
                if entry.get("error"):
                    last_error = str(entry.get("error") or "")
                messages = entry.get("messages") or []
                if messages:
                    stage_messages.setdefault(stage, []).extend(str(msg) for msg in messages)

        elif event_type == "harness.session_finished":
            if entry.get("error"):
                last_error = str(entry.get("error") or "")

    # 确定失败阶段
    for stage in META_EVOLVE_STAGE_ORDER:
        if stage_status.get(stage) == "failed":
            failed_stage = stage
            break

    # 确定当前阶段
    current_stage = ""
    if not failed_stage:
        for stage in META_EVOLVE_STAGE_ORDER:
            status = stage_status.get(stage)
            if status not in {"success", "skipped", "failed"}:
                if status == "running":
                    current_stage = stage
                elif status is None and current_stage:
                    break

    completed = [
        stage for stage in META_EVOLVE_STAGE_ORDER
        if stage_status.get(stage) in {"success", "skipped"}
    ]

    stages = []
    for stage in META_EVOLVE_STAGE_ORDER:
        status = stage_status.get(stage)
        if not status:
            status = "running" if stage == current_stage else "pending"
        stages.append({
            "stage": stage,
            "name": STAGE_DISPLAY_NAMES.get(stage, stage),
            "status": status,
            "messages": stage_messages.get(stage, [])[-3:] or [],
        })

    progress = {
        "stages": stages,
        "completed_stages": completed,
        "current_stage": current_stage,
        "failed_stage": failed_stage,
        "last_error": last_error,
        "failure_code": classify_failure(last_error, "") if failed_stage else "",
        "summary": format_progress_summary({
            "stages": stages,
            "completed_stages": completed,
            "current_stage": current_stage,
            "failed_stage": failed_stage,
        }),
    }
    return progress
