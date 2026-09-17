# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared helpers for skill evolution events and status pushes."""

from __future__ import annotations

import inspect
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from jiuwenswarm.server.runtime.skill import filter_visible_skill_names

logger = logging.getLogger(__name__)

_EVOLUTION_FILENAME = "evolutions.json"
TEAM_EVOLUTION_IDLE_SLEEP_SEC = 1.0
TEAM_EVOLUTION_EVENT_TIMEOUT_SEC = 900.0
TEAM_EVOLUTION_EVENT_TIMEOUT_GRACE_SEC = 5.0
TEAM_EVOLUTION_START_STAGE = "collecting"
TEAM_EVOLUTION_START_MESSAGE = "Running team skill evolution analysis..."
TEAM_EVOLUTION_NOOP_STAGE = "no_evolution_generated"
TEAM_EVOLUTION_NOOP_NO_SKILL_STAGE = "no_evolution_no_skill"
TEAM_EVOLUTION_NOOP_NO_SIGNAL_STAGE = "no_evolution_no_signal"
TEAM_EVOLUTION_NOOP_NO_RECORDS_STAGE = "no_evolution_no_records"
TEAM_EVOLUTION_HIDDEN_STAGE = "hidden"
TEAM_EVOLUTION_NOOP_MARKERS = (
    "no existing skill found",
    "no evolution signals detected",
    "no evolution records generated",
)
TEAM_EVOLUTION_NO_SKILL_MARKERS = (
    "no skill usage",
    "no existing skill",
    "no regular skill could be attributed",
    "no team/swarm skill",
)
TEAM_EVOLUTION_NO_SIGNAL_MARKERS = (
    "no actionable evolution signals detected",
    "no evolution signals detected",
)
TEAM_EVOLUTION_NOOP_STAGES = {
    TEAM_EVOLUTION_NOOP_STAGE,
    TEAM_EVOLUTION_NOOP_NO_SKILL_STAGE,
    TEAM_EVOLUTION_NOOP_NO_SIGNAL_STAGE,
    TEAM_EVOLUTION_NOOP_NO_RECORDS_STAGE,
}
TEAM_EVOLUTION_HIDDEN_TERMINAL_STAGES = {TEAM_EVOLUTION_HIDDEN_STAGE, "failed", "timed_out"}
TEAM_EVOLUTION_VISIBLE_PROGRESS_STAGES = {
    "generating",
    "approval_required",
    "completed",
    *TEAM_EVOLUTION_NOOP_STAGES,
}
REGULAR_EVOLUTION_VISIBLE_START_STAGES = {
    "generating",
    "approval_required",
    "completed",
}


@dataclass(frozen=True)
class EvolutionPushContext:
    transport: Any
    channel_id: str | None
    session_id: str


@dataclass(frozen=True)
class EvolutionStatusUpdate:
    request_id: str
    status: str
    stage: str
    message: str = ""


@dataclass(frozen=True)
class EvolutionProgressStatus:
    stage: str
    message: str = ""
    request_id: str | None = None
    terminal: bool = False


_SDK_PROGRESS_STAGE_MAP = {
    "started": "detecting",
    "detecting_signals": "detecting",
    "staging": "generating",
    "generating_updates": "generating",
    "approval_required": "approval_required",
    "auto_approved": "completed",
    "cancelled": TEAM_EVOLUTION_HIDDEN_STAGE,
    "completed": "completed",
    "failed": "failed",
    "timed_out": "timed_out",
}

_SDK_PROGRESS_TERMINAL_STAGES = {
    "auto_approved",
    "cancelled",
    "completed",
    "failed",
    "timed_out",
}

EVOLUTION_ACCEPT_LABELS = (
    "accept",
    "接收",
    "接受",
    "allow_once",
    "allow_always",
    "本次允许",
    "总是允许",
)
EVOLUTION_EXECUTE_LABELS = ("execute", "执行")
REGULAR_EVOLUTION_SLASH_WARNING_PHRASES = (
    "未生成可保存经验",
    "未发现明确的演进信号",
)
TEAM_EVOLUTION_SLASH_WARNING_PHRASES = (
    "未生成可保存经验",
    "未生成新的团队技能演进经验",
)
_EVOLUTION_SLASH_COMMANDS = (
    "evolve_simplify",
    "evolve_rebuild",
    "evolve_rollback",
    "evolve_list",
)


def _resolve_skill_dir(store: Any, skill_name: str) -> Path | None:
    resolver = getattr(store, "resolve_skill_dir", None)
    if callable(resolver):
        try:
            resolved = resolver(skill_name)
        except TypeError:
            resolved = None
        if resolved is not None:
            return Path(resolved)

    base_dirs = getattr(store, "base_dirs", None)
    if base_dirs is None:
        base_dir = getattr(store, "base_dir", None)
        base_dirs = [base_dir] if base_dir is not None else []

    for base_dir in base_dirs or []:
        candidate = Path(base_dir) / skill_name
        if candidate.is_dir():
            return candidate
    return None


def read_skill_kind(store: Any, skill_name: str) -> str | None:
    """Read the ``kind`` field from a skill's SKILL.md frontmatter."""
    skill_dir = _resolve_skill_dir(store, skill_name)
    if skill_dir is None:
        return None
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        content = skill_md.read_text(encoding="utf-8")
        m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if not m:
            return None
        fm = yaml.safe_load(m.group(1))
        if isinstance(fm, dict):
            kind = fm.get("kind")
            return str(kind).strip() if isinstance(kind, str) else None
    except Exception:
        return None
    return None


def _available_skill_names(store: Any) -> str:
    try:
        names = filter_visible_skill_names(store.list_skill_names())
    except Exception:
        names = []
    return "、".join(names) or "（无可用 Skill）"


def _skill_exists(store: Any, skill_name: str) -> bool:
    exists = getattr(store, "skill_exists", None)
    if callable(exists):
        try:
            return bool(exists(skill_name))
        except Exception:
            return False
    return _resolve_skill_dir(store, skill_name) is not None


def _skill_definition_exists(store: Any, skill_name: str) -> bool:
    checker = getattr(store, "skill_definition_exists", None)
    if callable(checker):
        try:
            return bool(checker(skill_name))
        except Exception:
            return False

    skill_dir = _resolve_skill_dir(store, skill_name)
    if skill_dir is None:
        return _skill_exists(store, skill_name)
    return (skill_dir / "SKILL.md").is_file()


def validate_evolution_skill(
    store: Any,
    skill_name: str,
    require_skill_md: bool,
) -> str | None:
    """Validate that an evolution command can target ``skill_name``."""
    if not _skill_exists(store, skill_name):
        return f"未找到 Skill '{skill_name}'。当前可用：{_available_skill_names(store)}"

    if require_skill_md:
        if not _skill_definition_exists(store, skill_name):
            return f"Skill '{skill_name}' 缺少 SKILL.md，无法执行演进生成。"

        skill_dir = _resolve_skill_dir(store, skill_name)
        if skill_dir is not None:
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists() and not os.access(skill_md, os.W_OK):
                return f"Skill '{skill_name}' 的 SKILL.md 不可写，无法执行演进。"

    return None


def validate_team_evolution_skill(
    store: Any,
    skill_name: str,
    require_skill_md: bool,
) -> str | None:
    """Validate that an evolution command can target ``skill_name`` in team mode."""
    base_error = validate_evolution_skill(store, skill_name, require_skill_md)
    if base_error is not None:
        return base_error

    kind = read_skill_kind(store, skill_name)
    if kind is not None and kind not in ("swarm-skill", "team-skill"):
        return f"集群模式下仅支持演进 Swarm Skill，指定 Skill '{skill_name}' 不是 Swarm Skill。"

    return None


def validate_evolution_log_writable(store: Any, skill_name: str) -> str | None:
    """Validate the local evolution log target is writable when it can be inspected."""
    skill_dir = _resolve_skill_dir(store, skill_name)
    if skill_dir is None:
        return None

    log_path = skill_dir / _EVOLUTION_FILENAME
    target = log_path if log_path.exists() else skill_dir
    if not os.access(target, os.W_OK):
        if log_path.exists():
            return f"Skill '{skill_name}' 的 evolutions.json 不可写，无法保存演进经验。"
        return f"Skill '{skill_name}' 目录不可写，无法保存演进经验。"
    return None


def evolution_status_response(
    evolve_result: Any,
    *,
    generation_failed_output: str,
    no_records_output: str,
) -> dict[str, str] | None:
    """Map SDK ``EvolutionRequestResult.status`` to a user-facing response."""
    status = str(getattr(evolve_result, "status", "") or "").strip()
    if not status:
        return None

    message = str(getattr(evolve_result, "message", "") or "").strip()

    if status == "generation_failed":
        output = _user_facing_generation_error(message) or generation_failed_output
        return {"output": output, "result_type": "error"}

    if status == "skipped_skill_definition_not_found":
        output = "Skill 缺少 SKILL.md，无法执行演进生成。"
        if message:
            output = f"{output}\n{message}"
        return {"output": output, "result_type": "error"}

    if status == "persistence_failed":
        output = "演进经验保存失败"
        if message:
            output = f"{output}：{message}"
        return {"output": output, "result_type": "error"}

    if status == "no_evolution_no_records":
        output = no_records_output
        if message:
            output = f"{output}\n{message}"
        return {"output": output, "result_type": "answer"}

    return None


def _user_facing_generation_error(message: str) -> str:
    """Hide low-level toolchain error chains from user-facing LLM failure text."""
    lowered = message.lower()
    internal_markers = (
        "toolchain",
        "tool_call",
        "invoke_failed",
        "execution error",
        "optimizer",
    )
    if any(marker in lowered for marker in internal_markers):
        return "LLM 服务调用失败，请检查模型配置或稍后重试"
    return message


def evolution_slash_display_level(
    result_type: str,
    content: str,
    *,
    warning_phrases: tuple[str, ...] = REGULAR_EVOLUTION_SLASH_WARNING_PHRASES,
) -> str:
    """Return frontend display severity for an evolution slash result."""
    if result_type == "error":
        return "error"
    if any(phrase in content for phrase in warning_phrases):
        return "warning"
    return "info"


def evolution_slash_command_name(query: str) -> str:
    """Return the evolution slash command name without the leading slash."""
    stripped = str(query or "").strip()
    for command in _EVOLUTION_SLASH_COMMANDS:
        if stripped.startswith(f"/{command}"):
            return command
    return "evolve"


def evolution_slash_result(
    command: str,
    result: dict[str, Any],
    *,
    warning_phrases: tuple[str, ...] = REGULAR_EVOLUTION_SLASH_WARNING_PHRASES,
) -> dict[str, Any]:
    """Annotate an evolution slash command result for frontend rendering."""
    annotated = dict(result)
    annotated.setdefault("source", "slash_command")
    annotated.setdefault("slash_command", command)
    if "display_level" not in annotated:
        result_type = str(annotated.get("result_type", "answer")).strip().lower()
        output = str(annotated.get("output", ""))
        annotated["display_level"] = evolution_slash_display_level(
            result_type,
            output,
            warning_phrases=warning_phrases,
        )
    return annotated


def event_payload_dict(evt: Any) -> dict[str, Any]:
    if hasattr(evt, "payload") and isinstance(evt.payload, dict):
        return dict(evt.payload)
    if isinstance(evt, dict):
        return dict(evt)
    return {}


def event_type(evt: Any) -> str:
    evt_type = getattr(evt, "type", None)
    if isinstance(evt_type, str) and evt_type:
        return evt_type
    payload = event_payload_dict(evt)
    payload_type = payload.get("event_type")
    return payload_type if isinstance(payload_type, str) else ""


def evolution_meta_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("evolution_meta", "_evolution_meta"):
        meta = payload.get(key)
        if isinstance(meta, dict):
            return meta
    return {}


def evolution_meta_from_params(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    return dict(evolution_meta_from_payload(params))


def answer_selects_option(
    answer: Any,
    labels: tuple[str, ...],
) -> bool:
    if not isinstance(answer, dict):
        return False
    normalized_labels = {label.strip().lower() for label in labels}
    for option in answer.get("selected_options", []) or []:
        if isinstance(option, str) and option.strip().lower() in normalized_labels:
            return True
    return False


def answers_select_option(answers: list[Any], labels: tuple[str, ...]) -> bool:
    return any(answer_selects_option(answer, labels) for answer in answers)


def approved_record_ids_from_answers(
    answers: list[Any],
    labels: tuple[str, ...],
    record_ids_by_index: list[str] | None = None,
) -> tuple[bool, list[str] | None]:
    """Map generic indexed answers back to SDK record ids when host state has them.

    Frontends intentionally remain unaware of evolution ``record_id`` fields.
    If no record ids are available, callers receive ``None`` for
    ``approved_record_ids`` and can preserve legacy all-or-nothing approval.
    """
    accepted = False
    approved_ids: list[str] = []
    seen_ids: set[str] = set()

    for index, answer in enumerate(answers):
        if not answer_selects_option(answer, labels):
            continue
        accepted = True

        record_id = ""
        if record_ids_by_index is not None and index < len(record_ids_by_index):
            record_id = str(record_ids_by_index[index] or "").strip()
        if not record_id or record_id in seen_ids:
            continue
        seen_ids.add(record_id)
        approved_ids.append(record_id)

    if not accepted:
        return False, []
    if record_ids_by_index is None:
        return True, None
    return True, approved_ids


def record_ids_from_pending_approval(
    rail: Any,
    request_id: str,
) -> list[str] | None:
    pending_snapshots = getattr(rail, "_pending_approval_snapshots", None)
    if not isinstance(pending_snapshots, dict):
        return None
    pending = pending_snapshots.get(request_id)
    payload = getattr(pending, "payload", None)
    if not isinstance(payload, list):
        return None

    record_ids: list[str] = []
    has_record_id = False
    for record in payload:
        raw_record_id = getattr(record, "id", None)
        if raw_record_id is None and isinstance(record, dict):
            raw_record_id = record.get("id") or record.get("record_id")
        record_id = str(raw_record_id or "").strip()
        record_ids.append(record_id)
        has_record_id = has_record_id or bool(record_id)
    return record_ids if has_record_id else None


async def approve_evolution_records(
    rail: Any,
    request_id: str,
    approved_record_ids: list[str] | None,
    *,
    legacy_fallback: bool = False,
) -> None:
    if hasattr(rail, "approve_record"):
        if approved_record_ids is None:
            await rail.approve_record(request_id)
        else:
            await rail.approve_record(
                request_id,
                approved_record_ids=approved_record_ids,
            )
        return

    if legacy_fallback:
        await rail.on_approve(request_id)
        return

    await rail.approve_record(request_id)


async def reject_evolution_records(
    rail: Any,
    request_id: str,
    *,
    legacy_fallback: bool = False,
) -> None:
    if hasattr(rail, "reject_record"):
        await rail.reject_record(request_id)
        return

    if legacy_fallback:
        await rail.on_reject(request_id)
        return

    await rail.reject_record(request_id)


def resolve_evolution_event_timeout_sec(
    rail: Any,
    *,
    fallback_sec: float | None = None,
    grace_sec: float | None = None,
) -> float:
    """Resolve host watcher timeout from the SDK rail's background evolution timeout."""
    fallback = TEAM_EVOLUTION_EVENT_TIMEOUT_SEC if fallback_sec is None else fallback_sec
    grace = TEAM_EVOLUTION_EVENT_TIMEOUT_GRACE_SEC if grace_sec is None else grace_sec

    try:
        sdk_timeout = getattr(rail, "evolution_total_timeout_secs", None)
    except Exception as exc:
        logger.debug("Failed to read SDK evolution timeout from property: %s", exc)
        return fallback

    try:
        parsed_timeout = float(sdk_timeout)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(parsed_timeout) or parsed_timeout <= 0:
        return fallback
    return parsed_timeout + max(grace, 0.0)


def is_evolution_approval_event(evt: Any) -> bool:
    if event_type(evt) == "chat.ask_user_question":
        return True
    payload = event_payload_dict(evt)
    return payload.get("event_type") == "chat.ask_user_question"


def evolution_event_kind(evt: Any) -> str:
    payload = event_payload_dict(evt)
    meta = evolution_meta_from_payload(payload)
    event_kind = meta.get("event_kind")
    if isinstance(event_kind, str) and event_kind:
        return event_kind
    if is_evolution_approval_event(evt):
        return "approval"
    return "stream"


def is_evolution_outcome_event(evt: Any) -> bool:
    return evolution_event_kind(evt) == "outcome"


def evolution_outcome_from_event(evt: Any) -> dict[str, str]:
    payload = event_payload_dict(evt)
    if not isinstance(payload, dict):
        return {"status": "completed", "message": str(payload)}
    meta = evolution_meta_from_payload(payload)
    meta_status = meta.get("status")
    status = (
        str(payload.get("status") or meta_status or "completed").strip().lower()
        or "completed"
    )
    message = str(payload.get("message") or payload.get("content") or "")
    return {"status": status, "message": message}


def extract_evolution_request_id(evt: Any) -> str | None:
    payload = event_payload_dict(evt)
    request_id = payload.get("request_id")
    if not request_id:
        meta = evolution_meta_from_payload(payload)
        request_id = meta.get("request_id")
    if isinstance(request_id, str):
        request_id = request_id.strip()
    return request_id or None


def evolution_progress_status_from_event(evt: Any) -> EvolutionProgressStatus | None:
    payload = event_payload_dict(evt)
    meta = evolution_meta_from_payload(payload)
    if not meta:
        return None
    event_kind = str(meta.get("event_kind") or "").strip().lower()
    if event_kind != "progress":
        return None
    raw_stage = str(payload.get("stage") or meta.get("stage") or "").strip().lower()
    if not raw_stage:
        return None
    message = str(payload.get("message") or payload.get("content") or "").strip()
    noop_stage = _noop_stage_from_message(message.lower())
    stage = (
        noop_stage
        if raw_stage != "cancelled" and noop_stage is not None
        else _SDK_PROGRESS_STAGE_MAP.get(raw_stage, raw_stage)
    )
    return EvolutionProgressStatus(
        stage=stage,
        message=message,
        request_id=extract_evolution_request_id(evt),
        terminal=raw_stage in _SDK_PROGRESS_TERMINAL_STAGES,
    )


def visible_evolution_progress_from_events(events: list[Any]) -> list[EvolutionProgressStatus]:
    return [
        progress
        for progress in (evolution_progress_status_from_event(evt) for evt in events)
        if progress is not None and progress.stage in TEAM_EVOLUTION_VISIBLE_PROGRESS_STAGES
    ]


def visible_regular_evolution_start_progress(
    progress_statuses: list[EvolutionProgressStatus],
) -> list[EvolutionProgressStatus]:
    return [
        progress
        for progress in progress_statuses
        if progress.stage in REGULAR_EVOLUTION_VISIBLE_START_STAGES
    ]


def progress_for_request(
    progress_statuses: list[EvolutionProgressStatus],
    request_id: str,
) -> list[EvolutionProgressStatus]:
    return [
        progress
        for progress in progress_statuses
        if progress.request_id is None or progress.request_id == request_id
    ]


def terminal_stage(terminal: dict[str, str]) -> str:
    return str(terminal.get("stage") or terminal.get("status") or "").strip().lower()


def terminal_progress_from_events(events: list[Any]) -> list[tuple[str | None, dict[str, str]]]:
    terminal_progress: list[tuple[str | None, dict[str, str]]] = []
    for evt in events:
        terminal = team_evolution_terminal_progress(evt)
        if terminal is not None:
            terminal_progress.append((extract_evolution_request_id(evt), terminal))
    return terminal_progress


def _noop_stage_from_message(message_lower: str) -> str | None:
    if any(marker in message_lower for marker in TEAM_EVOLUTION_NO_SKILL_MARKERS):
        return TEAM_EVOLUTION_NOOP_NO_SKILL_STAGE
    if any(marker in message_lower for marker in TEAM_EVOLUTION_NO_SIGNAL_MARKERS):
        return TEAM_EVOLUTION_NOOP_NO_SIGNAL_STAGE
    if "no evolution records generated" in message_lower:
        return TEAM_EVOLUTION_NOOP_NO_RECORDS_STAGE
    if any(marker in message_lower for marker in TEAM_EVOLUTION_NOOP_MARKERS):
        return TEAM_EVOLUTION_NOOP_STAGE
    return None


def team_evolution_terminal_progress(evt: Any) -> dict[str, str] | None:
    payload = event_payload_dict(evt)
    progress = evolution_progress_status_from_event(evt)
    if (
        progress is not None
        and progress.terminal
        and progress.stage == TEAM_EVOLUTION_HIDDEN_STAGE
    ):
        return {
            "status": progress.stage,
            "stage": progress.stage,
            "message": progress.message,
        }
    message = str(payload.get("message") or payload.get("content") or "")
    message_lower = message.lower()
    noop_stage = _noop_stage_from_message(message_lower)
    if noop_stage is not None:
        return {
            "status": "completed",
            "stage": noop_stage,
            "message": message or "No evolution generated",
        }
    if progress is not None and progress.terminal:
        if progress.stage == TEAM_EVOLUTION_NOOP_STAGE:
            return {
                "status": "completed",
                "stage": TEAM_EVOLUTION_NOOP_STAGE,
                "message": progress.message or "No evolution generated",
            }
        return {
            "status": progress.stage,
            "stage": progress.stage,
            "message": progress.message,
        }
    meta = evolution_meta_from_payload(payload)
    meta_status = meta.get("status")
    meta_stage = meta.get("stage")
    status = str(payload.get("status") or meta_status or "").strip().lower()
    stage = str(payload.get("stage") or meta_stage or "").strip().lower()
    if status == "end" or stage in {"completed", "failed", "timed_out"}:
        return {
            "status": status or "end",
            "stage": stage or "completed",
            "message": message,
        }
    return None


def build_evolution_status_update(
    request_id: str,
    status: str,
    stage: str,
    message: str = "",
) -> EvolutionStatusUpdate:
    return EvolutionStatusUpdate(
        request_id=request_id,
        status=status,
        stage=stage,
        message=message,
    )


def team_evolution_end_update(
    request_id: str,
    terminal: dict[str, str] | None,
) -> EvolutionStatusUpdate:
    if terminal is None:
        return build_evolution_status_update(
            request_id=request_id,
            status="end",
            stage="completed",
            message="Team skill evolution analysis completed",
        )
    stage = str(terminal.get("stage") or terminal.get("status") or "completed").strip().lower()
    message = str(terminal.get("message") or "")
    if stage in {"failed", "timed_out"}:
        return build_evolution_status_update(
            request_id=request_id,
            status="end",
            stage=TEAM_EVOLUTION_HIDDEN_STAGE,
            message=message,
        )
    if stage in TEAM_EVOLUTION_NOOP_STAGES:
        return build_evolution_status_update(
            request_id=request_id,
            status="end",
            stage=stage,
            message=message,
        )
    return build_evolution_status_update(
        request_id=request_id,
        status="end",
        stage=stage or "completed",
        message=message or "Team skill evolution analysis completed",
    )


def group_evolution_approvals(
    session_id: str,
    events: list[Any],
    *,
    warn_missing_request_id: Callable[[str], None] | None = None,
) -> tuple[dict[str, list[Any]], list[str]]:
    grouped: dict[str, list[Any]] = {}
    for evt in events:
        if not is_evolution_approval_event(evt):
            continue
        request_id = extract_evolution_request_id(evt)
        if request_id is None:
            if warn_missing_request_id is not None:
                warn_missing_request_id(session_id)
            continue
        grouped.setdefault(request_id, []).append(evt)
    return grouped, []


def make_team_evolution_cycle_request_id(session_id: str, cycle_index: int) -> str:
    return f"team_evolve_{session_id}_{cycle_index}"


async def push_evolution_status(
    push_context: EvolutionPushContext,
    status_update: EvolutionStatusUpdate,
    build_push_message: Callable[..., dict[str, Any]],
    *,
    include_payload_request_id: bool = True,
) -> None:
    payload = {
        "event_type": "chat.evolution_status",
        "status": status_update.status,
        "stage": status_update.stage,
        "message": status_update.message,
    }
    if include_payload_request_id:
        payload["request_id"] = status_update.request_id
    await push_context.transport.send_push(
        build_push_message(
            session_id=push_context.session_id,
            request_id=status_update.request_id,
            fallback_channel_id=push_context.channel_id,
            payload=payload,
        )
    )


async def push_evolution_event(
    push_context: EvolutionPushContext,
    request_id: str,
    evt: Any,
    build_push_message: Callable[..., dict[str, Any]],
) -> None:
    payload = event_payload_dict(evt)
    evt_type = event_type(evt)
    if evt_type and "event_type" not in payload:
        payload["event_type"] = evt_type
    payload.setdefault("request_id", request_id)
    await push_context.transport.send_push(
        build_push_message(
            session_id=push_context.session_id,
            request_id=request_id,
            fallback_channel_id=push_context.channel_id,
            payload=payload,
        )
    )


async def broadcast_evolution_progress(
    channel_id: str | None,
    session_id: str,
    events: list[Any],
    *,
    parse_stream_chunk: Callable[[Any], dict[str, Any] | None],
    broadcast_event: Callable[[str | None, str, dict[str, Any]], Any],
) -> None:
    for evt in events:
        if (
            is_evolution_approval_event(evt)
            or is_evolution_outcome_event(evt)
            or team_evolution_terminal_progress(evt) is not None
        ):
            continue
        parsed = parse_stream_chunk(evt)
        if parsed is not None:
            result = broadcast_event(channel_id, session_id, parsed)
            if inspect.isawaitable(result):
                await result


async def push_evolution_progress(
    push_context: EvolutionPushContext,
    request_id: str,
    events: list[Any],
    *,
    parse_stream_chunk: Callable[[Any], dict[str, Any] | None],
    build_push_message: Callable[..., dict[str, Any]],
) -> None:
    for evt in events:
        if (
            is_evolution_approval_event(evt)
            or is_evolution_outcome_event(evt)
            or team_evolution_terminal_progress(evt) is not None
        ):
            continue
        try:
            parsed = parse_stream_chunk(evt)
            if parsed is None:
                continue
            await push_context.transport.send_push(
                build_push_message(
                    session_id=push_context.session_id,
                    request_id=request_id,
                    fallback_channel_id=push_context.channel_id,
                    payload=parsed,
                )
            )
        except Exception as exc:
            logger.warning(
                "Failed to push evolution progress: request_id=%s session_id=%s error=%s",
                request_id,
                push_context.session_id,
                exc,
            )
