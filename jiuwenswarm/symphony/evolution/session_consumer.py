"""Learn Symphony runtime outcomes from durable session history."""

from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.session.session_history import (
    SESSION_REQUEST_COMPLETED_EVENT,
    enqueue_history_request_completion,
    get_read_history_path,
    load_history_records,
)
from jiuwenswarm.symphony.config import load_symphony_config
from jiuwenswarm.symphony.evolution.models import normalize_edges, skill_id
from jiuwenswarm.symphony.evolution.service import (
    prepare_evolution_store,
    record_plan_outcome,
)

LOGGER = logging.getLogger(__name__)

SESSION_FEEDBACK_SCHEMA_VERSION = "symphony.session_feedback.v1"
SESSION_FEEDBACK_STATE_FILE = "session_feedback_state.json"
SESSION_FEEDBACK_SOURCE = "session_history"
SESSION_INFERENCE_VERSION = "session_history_v1"
_MAX_TRACKED_SESSIONS = 500
_MAX_TRACKED_SKILLS = 200
_MAX_SKIPPED_TURNS = 3
_SYMPHONY_TOOL_NAMES = {
    "symphony_compose_graph",
    "symphony_read_graph",
    "symphony_refresh_graph",
}
_SKILL_FILE_TOOL_NAMES = {"read_file", "Read"}
_NON_BUSINESS_TOOL_NAMES = {
    "skill_tool",
    *_SKILL_FILE_TOOL_NAMES,
    *_SYMPHONY_TOOL_NAMES,
}
_TERMINAL_RESPONSE_EVENTS = {
    "chat.final",
    "chat.error",
    "chat.ask_user_question",
}
_CONTINUATION_RE = re.compile(
    r"(?:"
    r"确认.{0,12}(?:执行|继续|开始)|"
    r"按(?:照)?(?:上面|上述|这个|该)?(?:的)?"
    r"(?:路径|计划|方案|技能)?.{0,12}(?:执行|继续|做)|"
    r"继续(?:执行|完成)|开始执行|直接执行|就按.{0,16}(?:执行|做)|"
    r"用(?:上面|上述|这个|该)?.{0,16}技能.{0,8}执行|"
    r"\b(?:confirm|proceed|continue|execute|run|go ahead|follow the plan)\b"
    r")",
    re.IGNORECASE,
)
_SKILL_FRONTMATTER_NAME_RE = re.compile(
    r"---[ \t]*(?:\r?\n|\\n)[ \t]*name[ \t]*:[ \t]*[\"']?"
    r"([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
_NON_EXECUTION_FINAL_RE = re.compile(
    r"^\s*(?:(?:已|本次|当前)\s*)?"
    r"(?:取消(?:执行|计划|任务)?|未执行|无法执行|需要.{0,16}(?:补充|提供)|"
    r"(?:cancelled|canceled|not executed|unable to execute|need(?:s|ed)? more input)\b)",
    re.IGNORECASE,
)

_STATE_LOCK = threading.RLock()
_SCHEDULE_LOCK = threading.Lock()
_SCHEDULED: set[tuple[str, str, str]] = set()
_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="symphony-session-feedback",
)


def session_feedback_state_path(graph_dir: str | Path) -> Path:
    return Path(graph_dir) / "evolution" / SESSION_FEEDBACK_STATE_FILE


def schedule_session_evolution_consume(
    session_id: str,
    request_id: str,
    *,
    terminal_status: str = "success",
) -> bool:
    """Schedule non-blocking feedback consumption after history is durable."""

    clean_session_id = str(session_id or "").strip()
    clean_request_id = str(request_id or "").strip()
    if not clean_session_id or not clean_request_id:
        return False
    try:
        config = load_symphony_config()
        if not config.enabled or not config.evolution.enabled:
            return False
        graph_dir = prepare_evolution_store(config.paths.graph_dir)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("Symphony session feedback is unavailable: %s", exc)
        return False

    schedule_key = (str(graph_dir), clean_session_id, clean_request_id)
    with _SCHEDULE_LOCK:
        if schedule_key in _SCHEDULED:
            return False
        _SCHEDULED.add(schedule_key)

    try:
        completion_receipt = enqueue_history_request_completion(
            clean_session_id,
            clean_request_id,
            terminal_status=terminal_status,
        )
        if completion_receipt is None:
            raise RuntimeError("request completion was not enqueued")
        future = _EXECUTOR.submit(
            _consume_after_history_ready,
            clean_session_id,
            clean_request_id,
            graph_dir,
            completion_receipt,
        )
    except Exception as exc:  # noqa: BLE001
        with _SCHEDULE_LOCK:
            _SCHEDULED.discard(schedule_key)
        LOGGER.warning("Failed to schedule Symphony session feedback: %s", exc)
        return False
    future.add_done_callback(lambda current: _on_consume_done(schedule_key, current))
    return True


def _consume_after_history_ready(
    session_id: str,
    request_id: str,
    graph_dir: Path,
    completion_receipt: Future[None],
) -> dict[str, Any]:
    completion_receipt.result()
    history_path = get_read_history_path(session_id)
    return consume_session_history(
        session_id,
        completed_request_id=request_id,
        graph_dir=graph_dir,
        history_limit_bytes=_file_size(history_path),
    )


def consume_session_history(
    session_id: str,
    *,
    completed_request_id: str = "",
    graph_dir: str | Path | None = None,
    history_limit_bytes: int | None = None,
) -> dict[str, Any]:
    """Incrementally consume one session and emit at most one outcome per plan."""

    clean_session_id = str(session_id or "").strip()
    if not clean_session_id:
        return {"success": False, "detail": "session_id is required"}
    if graph_dir is None:
        config = load_symphony_config()
        if not config.enabled or not config.evolution.enabled:
            return {
                "success": True,
                "enabled": False,
                "recorded": False,
                "source": SESSION_FEEDBACK_SOURCE,
            }
        graph_dir = config.paths.graph_dir
    resolved_graph_dir = prepare_evolution_store(graph_dir)
    history_path = get_read_history_path(clean_session_id)

    with _STATE_LOCK:
        state = _read_state(resolved_graph_dir)
        sessions = state.setdefault("sessions", {})
        session_state = dict(sessions.get(clean_session_id) or {})
        try:
            _bootstrap_activated_skill_state(
                clean_session_id,
                history_path,
                session_state,
            )
            records, cursor = _read_new_records(
                history_path,
                session_state,
                completed_request_id=completed_request_id,
                history_limit_bytes=history_limit_bytes,
            )
            results = _consume_records(
                records,
                session_id=clean_session_id,
                session_state=session_state,
                graph_dir=resolved_graph_dir,
            )
            session_state.update(cursor)
            for key in ("deferred_records", "deferred_completed_request_ids"):
                if not session_state.get(key):
                    session_state.pop(key, None)
            session_state["updated_at"] = _utc_now()
            sessions[clean_session_id] = session_state
            _update_state_stats(state, records, results)
            state["last_error"] = ""
            state["updated_at"] = _utc_now()
            _prune_sessions(sessions)
            _write_state(resolved_graph_dir, state)
        except Exception as exc:  # noqa: BLE001
            state["last_error"] = str(exc)[:1000]
            state["updated_at"] = _utc_now()
            _write_state(resolved_graph_dir, state)
            LOGGER.exception(
                "Failed to consume Symphony session feedback: session_id=%s request_id=%s",
                clean_session_id,
                completed_request_id,
            )
            return {
                "success": False,
                "source": SESSION_FEEDBACK_SOURCE,
                "session_id": clean_session_id,
                "request_id": completed_request_id,
                "detail": str(exc),
            }

    return {
        "success": True,
        "source": SESSION_FEEDBACK_SOURCE,
        "session_id": clean_session_id,
        "request_id": completed_request_id,
        "records_consumed": len(records),
        "outcomes": results,
    }


def session_feedback_status(graph_dir: str | Path) -> dict[str, Any]:
    """Return frontend-safe observability for the session feedback worker."""

    resolved_graph_dir = Path(graph_dir).resolve()
    with _STATE_LOCK:
        state = _read_state(resolved_graph_dir)
    sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    stats = state.get("stats") if isinstance(state.get("stats"), dict) else {}
    with _SCHEDULE_LOCK:
        pending_jobs = sum(
            1 for item in _SCHEDULED if item[0] == str(resolved_graph_dir)
        )
    return {
        "available": True,
        "source": SESSION_FEEDBACK_SOURCE,
        "mode": "incremental_async",
        "state_path": str(session_feedback_state_path(resolved_graph_dir)),
        "tracked_session_count": len(sessions),
        "pending_plan_count": sum(
            1
            for item in sessions.values()
            if isinstance(item, dict) and isinstance(item.get("pending_plan"), dict)
        ),
        "pending_job_count": pending_jobs,
        "records_consumed": int(stats.get("records_consumed") or 0),
        "plans_observed": int(stats.get("plans_observed") or 0),
        "outcomes_recorded": int(stats.get("outcomes_recorded") or 0),
        "duplicates_ignored": int(stats.get("duplicates_ignored") or 0),
        "last_result": dict(state.get("last_result") or {}),
        "last_error": str(state.get("last_error") or ""),
        "updated_at": str(state.get("updated_at") or ""),
    }


def _consume_records(
    records: list[dict[str, Any]],
    *,
    session_id: str,
    session_state: dict[str, Any],
    graph_dir: Path,
) -> list[dict[str, Any]]:
    pending = (
        dict(session_state.get("pending_plan") or {})
        if isinstance(session_state.get("pending_plan"), dict)
        else {}
    )
    activated_skill_ids = _dedupe_skill_ids(
        session_state.get("activated_skill_ids") or []
    )
    results: list[dict[str, Any]] = []
    for request_id, request_records in _group_by_request(records):
        activated_skill_ids = _merge_skill_ids(
            activated_skill_ids,
            _activated_skill_ids(request_records),
        )
        markers = _plan_markers(request_records, graph_dir=graph_dir)

        if markers:
            # A new plan supersedes an older plan that the user never executed.
            pending = {}
        elif pending:
            result = _consume_execution_turn(
                pending,
                request_records,
                session_id=session_id,
                request_id=request_id,
                graph_dir=graph_dir,
                same_turn=False,
                activated_skill_ids=activated_skill_ids,
            )
            if result is not None:
                results.append(result)
                pending = {}
            elif _has_user_record(request_records):
                skipped = int(pending.get("skipped_turns") or 0) + 1
                if skipped >= _MAX_SKIPPED_TURNS:
                    pending = {}
                else:
                    pending["skipped_turns"] = skipped

        for marker_index, marker in markers:
            pending = marker
            trailing_records = request_records[marker_index + 1:]
            result = _consume_execution_turn(
                pending,
                trailing_records,
                session_id=session_id,
                request_id=request_id,
                graph_dir=graph_dir,
                same_turn=True,
                activated_skill_ids=activated_skill_ids,
            )
            if result is not None:
                results.append(result)
                pending = {}

    if pending:
        session_state["pending_plan"] = pending
    else:
        session_state.pop("pending_plan", None)
    if activated_skill_ids:
        session_state["activated_skill_ids"] = activated_skill_ids
    else:
        session_state.pop("activated_skill_ids", None)
    return results


def _consume_execution_turn(
    pending: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    session_id: str,
    request_id: str,
    graph_dir: Path,
    same_turn: bool,
    activated_skill_ids: list[str],
) -> dict[str, Any] | None:
    classification = _classify_outcome(records)
    if classification is None:
        return None
    outcome, failure_type, detail = classification
    observed_skills = _observed_skill_ids(
        records,
        pending.get("selected_skill_ids") or [],
        include_attempted=outcome != "success",
    )
    observed_tools = _observed_tool_names(records)
    planned_skill_ids = _dedupe_skill_ids(pending.get("selected_skill_ids") or [])
    activated_planned_skills = _match_planned_skills(
        planned_skill_ids,
        activated_skill_ids,
    )
    correlation = _execution_correlation(
        pending,
        records,
        same_turn=same_turn,
        outcome=outcome,
        observed_skill_ids=observed_skills,
        activated_skill_ids=activated_planned_skills,
    )
    if not correlation:
        return None
    attributed_skills = (
        planned_skill_ids
        if correlation.startswith("session_activation_with_")
        else observed_skills
    )
    if outcome == "success" and not _all_planned_skills_observed(
        planned_skill_ids,
        attributed_skills,
    ):
        return None
    planned_edges = normalize_edges(pending.get("selected_edges") or [])
    selected_edges = _observed_edges(planned_edges, attributed_skills)
    failed_edges = selected_edges[-1:] if outcome == "failure" else []
    evidence_id = f"session:{session_id}:{pending['plan_id']}:{request_id}"
    event = record_plan_outcome(
        graph_dir,
        plan_id=str(pending["plan_id"]),
        query=str(pending.get("query") or ""),
        outcome=outcome,
        selected_skill_ids=attributed_skills,
        selected_edges=selected_edges,
        failed_edges=failed_edges,
        failure_attribution="terminal_edge" if outcome == "failure" else "",
        failure_type=failure_type,
        detail=detail,
        source=SESSION_FEEDBACK_SOURCE,
        evidence_id=evidence_id,
        session_id=session_id,
        request_id=request_id,
        evidence={
            "inference_version": SESSION_INFERENCE_VERSION,
            "correlation": correlation,
            "planned_skill_ids": planned_skill_ids,
            "planned_edges": planned_edges,
            "observed_skill_ids": observed_skills,
            "activated_skill_ids": activated_planned_skills,
            "skill_evidence": (
                "session_activation"
                if correlation.startswith("session_activation_with_")
                else "current_request"
            ),
            "observed_tool_names": observed_tools,
            "same_turn": same_turn,
        },
    )
    return {
        "plan_id": str(pending["plan_id"]),
        "session_id": session_id,
        "request_id": request_id,
        "outcome": outcome,
        "correlation": correlation,
        "event_id": str(event.get("event_id") or ""),
        "evidence_id": evidence_id,
        "deduplicated": bool(event.get("deduplicated")),
        "processed_at": _utc_now(),
    }


def _plan_markers(
    records: list[dict[str, Any]],
    *,
    graph_dir: Path,
) -> list[tuple[int, dict[str, Any]]]:
    output: list[tuple[int, dict[str, Any]]] = []
    for index, record in enumerate(records):
        if record.get("event_type") != "chat.tool_result":
            continue
        if str(record.get("tool_name") or "") != "symphony_compose_graph":
            continue
        raw_output = record.get("raw_output")
        if not isinstance(raw_output, dict):
            continue
        if raw_output.get("dynamic_graph_enabled") is False:
            continue
        plan = raw_output.get("plan")
        if not isinstance(plan, dict) or str(plan.get("status") or "").lower() != "ready":
            continue
        plan_id = str(raw_output.get("plan_id") or "").strip()
        if not plan_id or not _graph_dir_matches(raw_output.get("graph_dir"), graph_dir):
            continue
        selected_skill_ids = []
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            current = skill_id(step.get("skill_id") or step.get("id")).strip()
            if current and current not in selected_skill_ids:
                selected_skill_ids.append(current)
        output.append(
            (
                index,
                {
                    "plan_id": plan_id,
                    "query": str(raw_output.get("query") or _user_text(records)).strip(),
                    "planning_request_id": str(record.get("request_id") or ""),
                    "selected_skill_ids": selected_skill_ids,
                    "selected_edges": normalize_edges(plan.get("can_feed_edges") or []),
                    "planned_at": record.get("timestamp"),
                    "skipped_turns": 0,
                },
            )
        )
    return output


def _execution_correlation(
    pending: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    same_turn: bool,
    outcome: str,
    observed_skill_ids: list[str],
    activated_skill_ids: list[str],
) -> str:
    if not records:
        return ""
    planned_skill_ids = list(pending.get("selected_skill_ids") or [])
    if _all_planned_skills_observed(planned_skill_ids, observed_skill_ids):
        return "planned_skill_observed"
    observed_tools = _observed_tool_names(records)
    has_execution_tool = any(
        name and name not in _SYMPHONY_TOOL_NAMES and name != "skill_tool"
        for name in observed_tools
    )
    activation_covers_plan = _all_planned_skills_observed(
        planned_skill_ids,
        activated_skill_ids,
    )
    has_successful_execution_tool = bool(_successful_execution_tool_names(records))
    if same_turn:
        if outcome == "success" and activation_covers_plan:
            if has_successful_execution_tool:
                return "session_activation_with_tool_execution"
            current_request_activations = _match_planned_skills(
                planned_skill_ids,
                _activated_skill_ids(records),
            )
            if (
                len(planned_skill_ids) == 1
                and _all_planned_skills_observed(
                    planned_skill_ids,
                    current_request_activations,
                )
                and _has_substantive_final(records)
            ):
                return "session_activation_with_final_response"
        return "same_turn_tool_execution" if has_execution_tool else ""
    if not _CONTINUATION_RE.search(_user_text(records)):
        return ""
    if outcome == "success" and activation_covers_plan:
        if has_successful_execution_tool:
            return "session_activation_with_tool_execution"
        if len(planned_skill_ids) == 1 and _has_substantive_final(records):
            return "session_activation_with_final_response"
    if has_execution_tool:
        return "explicit_continuation_with_tool_execution"
    if any(record.get("event_type") == "chat.error" for record in records):
        return "explicit_continuation_with_error"
    if any(
        record.get("event_type") == "chat.ask_user_question"
        for record in records
    ):
        return "explicit_continuation_with_needs_input"
    return ""


def _classify_outcome(
    records: list[dict[str, Any]],
) -> tuple[str, str, str] | None:
    if _request_cancelled(records):
        return None
    for record in records:
        if record.get("event_type") == "chat.error":
            error = str(record.get("error") or record.get("content") or "execution failed")
            return "failure", str(record.get("error_type") or "agent_error"), error[:1000]
    for record in records:
        if record.get("event_type") != "chat.tool_result":
            continue
        if _tool_result_failed(record):
            tool_name = str(record.get("tool_name") or "tool")
            detail = str(record.get("error") or record.get("result") or "tool execution failed")
            return "failure", f"{tool_name}_failed", detail[:1000]
    if any(record.get("event_type") == "chat.ask_user_question" for record in records):
        return "needs_input", "missing_input", "execution paused for user input"
    final_text = "\n".join(
        str(record.get("content") or "").strip()
        for record in records
        if record.get("event_type") == "chat.final" and str(record.get("content") or "").strip()
    )
    if final_text:
        if _NON_EXECUTION_FINAL_RE.search(final_text):
            return None
        return "success", "", final_text[-1000:]
    return None


def _request_cancelled(records: list[dict[str, Any]]) -> bool:
    return any(
        record.get("event_type") == SESSION_REQUEST_COMPLETED_EVENT
        and str(record.get("status") or "").strip().lower()
        in {"cancelled", "canceled", "aborted", "interrupted"}
        for record in records
    )


def _tool_result_failed(record: dict[str, Any]) -> bool:
    if record.get("success") is False or record.get("is_error") is True:
        return True
    status = str(record.get("status") or "").strip().lower()
    if status in {"error", "failed", "failure"}:
        return True
    result = record.get("raw_output")
    if result is None:
        result = record.get("result")
    return _value_reports_failure(result)


def _value_reports_failure(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("success") is False or value.get("is_error") is True:
            return True
        if str(value.get("status") or "").lower() in {"error", "failed", "failure"}:
            return True
        return any(
            _value_reports_failure(value.get(key))
            for key in ("data", "raw_output", "rawOutput", "result")
            if key in value
        )
    if isinstance(value, list):
        return any(_value_reports_failure(item) for item in value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, (dict, list)) and _value_reports_failure(parsed):
            return True
        return bool(
            re.search(r"\bsuccess\s*[:=]\s*False\b", text, re.IGNORECASE)
            or text.startswith("[ERROR]")
        )
    return False


def _observed_skill_ids(
    records: list[dict[str, Any]],
    selected_skill_ids: list[str],
    *,
    include_attempted: bool = True,
) -> list[str]:
    selected = {
        skill_id(item).strip().lower(): skill_id(item).strip()
        for item in selected_skill_ids
    }
    observed: list[str] = []
    attempted: list[str] = []
    pending_calls: list[tuple[str, str]] = []
    for record in records:
        event_type = record.get("event_type")
        if event_type == "chat.tool_call":
            tool_call = record.get("tool_call")
            if not isinstance(tool_call, dict):
                continue
            name = str(tool_call.get("name") or "").strip()
            if name != "skill_tool":
                continue
            arguments = _tool_arguments(tool_call.get("arguments"))
            candidate = str(arguments.get("skill_name") or "").strip()
            if not candidate:
                continue
            call_id = str(tool_call.get("tool_call_id") or "").strip()
            pending_calls.append((call_id, candidate))
            attempted.append(candidate)
            continue
        if event_type != "chat.tool_result":
            continue
        if str(record.get("tool_name") or "").strip() != "skill_tool":
            continue
        call_id = str(record.get("tool_call_id") or "").strip()
        invoked_name = _take_pending_skill_call(pending_calls, call_id)
        canonical_name = _skill_name_from_result(record)
        matched = _match_selected_skill(canonical_name, selected)
        if not matched:
            matched = _match_selected_skill(invoked_name, selected)
        if matched and matched not in observed:
            observed.append(matched)
    if include_attempted:
        for candidate in attempted:
            matched = _match_selected_skill(candidate, selected)
            if matched and matched not in observed:
                observed.append(matched)
    return observed


def _activated_skill_ids(records: list[dict[str, Any]]) -> list[str]:
    """Return Skills made available by a successful, paired tool result."""

    activated: list[str] = []
    pending_calls: list[tuple[str, str, str]] = []
    for record in records:
        if record.get("event_type") == "chat.tool_call":
            tool_call = record.get("tool_call")
            if not isinstance(tool_call, dict):
                continue
            tool_name = str(tool_call.get("name") or "").strip()
            arguments = _tool_arguments(tool_call.get("arguments"))
            candidate = ""
            if tool_name == "skill_tool":
                candidate = str(arguments.get("skill_name") or "").strip()
            elif tool_name in _SKILL_FILE_TOOL_NAMES:
                candidate = _managed_skill_name_from_path(
                    arguments.get("path")
                    or arguments.get("file_path")
                    or arguments.get("filepath")
                )
            if candidate:
                call_id = str(
                    tool_call.get("tool_call_id") or tool_call.get("id") or ""
                ).strip()
                pending_calls.append((call_id, tool_name, candidate))
            continue
        if record.get("event_type") != "chat.tool_result":
            continue
        tool_name = str(record.get("tool_name") or "").strip()
        if tool_name != "skill_tool" and tool_name not in _SKILL_FILE_TOOL_NAMES:
            continue
        invoked_name = _take_pending_activation_call(
            pending_calls,
            str(record.get("tool_call_id") or "").strip(),
            tool_name,
        )
        if not invoked_name or _tool_result_failed(record):
            continue
        canonical_name = _skill_name_from_result(record)
        if tool_name in _SKILL_FILE_TOOL_NAMES and (
            not canonical_name
            or not _skill_names_equivalent(invoked_name, canonical_name)
        ):
            continue
        candidate = skill_id(canonical_name or invoked_name).strip()
        if candidate and candidate.lower() not in {item.lower() for item in activated}:
            activated.append(candidate)
    return activated


def _take_pending_activation_call(
    pending_calls: list[tuple[str, str, str]],
    call_id: str,
    tool_name: str,
) -> str:
    if call_id:
        for index, (pending_id, pending_tool, candidate) in enumerate(pending_calls):
            if pending_id == call_id and pending_tool == tool_name:
                pending_calls.pop(index)
                return candidate
        return ""
    matches = [
        (index, candidate)
        for index, (_pending_id, pending_tool, candidate) in enumerate(pending_calls)
        if pending_tool == tool_name
    ]
    if len(matches) == 1:
        index, candidate = matches[0]
        pending_calls.pop(index)
        return candidate
    return ""


def _managed_skill_name_from_path(value: Any) -> str:
    raw_path = str(value or "").strip()
    if not raw_path:
        return ""
    try:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            return ""
        candidate = candidate.resolve()
        skills_root = load_symphony_config().paths.skills_root.resolve()
        relative = candidate.relative_to(skills_root)
    except (OSError, RuntimeError, ValueError):
        return ""
    if candidate.name != "SKILL.md" or len(relative.parts) < 2:
        return ""
    return candidate.parent.name


def _skill_names_equivalent(path_name: str, canonical_name: str) -> bool:
    path_id = skill_id(path_name).strip().lower()
    canonical_id = skill_id(canonical_name).strip().lower()
    return bool(
        path_id
        and canonical_id
        and (
            path_id == canonical_id
            or re.fullmatch(
                rf"{re.escape(canonical_id)}-v?\d+(?:\.\d+)*",
                path_id,
            )
        )
    )


def _take_pending_skill_call(
    pending_calls: list[tuple[str, str]],
    call_id: str,
) -> str:
    if call_id:
        for index, (pending_id, candidate) in enumerate(pending_calls):
            if pending_id == call_id:
                pending_calls.pop(index)
                return candidate
    if pending_calls:
        return pending_calls.pop(0)[1]
    return ""


def _skill_name_from_result(record: dict[str, Any]) -> str:
    for value in (record.get("raw_output"), record.get("result")):
        skill_content = _find_skill_content(value)
        if skill_content:
            match = _SKILL_FRONTMATTER_NAME_RE.search(skill_content)
            if match:
                return match.group(1).strip()
        if isinstance(value, str):
            match = _SKILL_FRONTMATTER_NAME_RE.search(value)
            if match:
                return match.group(1).strip()
    return ""


def _find_skill_content(value: Any) -> str:
    if isinstance(value, dict):
        direct = value.get("skill_content")
        if isinstance(direct, str):
            return direct
        for key in (
            "data",
            "raw_output",
            "rawOutput",
            "result",
            "content",
            "text",
            "output",
        ):
            nested = _find_skill_content(value.get(key))
            if nested:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = _find_skill_content(item)
            if nested:
                return nested
    return ""


def _match_selected_skill(
    candidate: str,
    selected: dict[str, str],
) -> str:
    normalized = skill_id(candidate).strip().lower()
    if not normalized:
        return ""
    exact = selected.get(normalized)
    if exact:
        return exact
    for selected_key, selected_value in selected.items():
        package_pattern = rf"{re.escape(selected_key)}-v?\d+(?:\.\d+)*"
        if re.fullmatch(package_pattern, normalized):
            return selected_value
    return ""


def _observed_tool_names(records: list[dict[str, Any]]) -> list[str]:
    output: list[str] = []
    for record in records:
        name = ""
        if record.get("event_type") == "chat.tool_call":
            tool_call = record.get("tool_call")
            if isinstance(tool_call, dict):
                name = str(tool_call.get("name") or "").strip()
        elif record.get("event_type") == "chat.tool_result":
            name = str(record.get("tool_name") or "").strip()
        if name and name not in output:
            output.append(name)
    return output


def _successful_execution_tool_names(records: list[dict[str, Any]]) -> list[str]:
    output: list[str] = []
    for record in records:
        if record.get("event_type") != "chat.tool_result":
            continue
        name = str(record.get("tool_name") or "").strip()
        if not name or name in _NON_BUSINESS_TOOL_NAMES:
            continue
        if _tool_result_failed(record):
            continue
        if name not in output:
            output.append(name)
    return output


def _has_substantive_final(records: list[dict[str, Any]]) -> bool:
    return any(
        record.get("event_type") == "chat.final"
        and bool(str(record.get("content") or "").strip())
        and not _NON_EXECUTION_FINAL_RE.search(str(record.get("content") or ""))
        for record in records
    )


def _dedupe_skill_ids(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        current = skill_id(value).strip()
        key = current.lower()
        if current and key not in seen:
            output.append(current)
            seen.add(key)
    return output


def _merge_skill_ids(existing: list[str], additions: list[str]) -> list[str]:
    return _dedupe_skill_ids([*existing, *additions])[-_MAX_TRACKED_SKILLS:]


def _match_planned_skills(
    planned_skill_ids: list[str],
    activated_skill_ids: list[str],
) -> list[str]:
    selected = {
        skill_id(item).strip().lower(): skill_id(item).strip()
        for item in planned_skill_ids
        if skill_id(item).strip()
    }
    output: list[str] = []
    for candidate in activated_skill_ids:
        matched = _match_selected_skill(candidate, selected)
        if matched and matched not in output:
            output.append(matched)
    return output


def _observed_edges(
    planned_edges: list[dict[str, Any]],
    observed_skill_ids: list[str],
) -> list[dict[str, Any]]:
    observed = {skill_id(item).strip() for item in observed_skill_ids}
    if len(observed) < 2:
        return []
    output = []
    for edge in planned_edges:
        source_id = skill_id(edge.get("source_id")).strip()
        target_id = skill_id(edge.get("target_id")).strip()
        if source_id in observed and target_id in observed:
            output.append(edge)
    return output


def _all_planned_skills_observed(
    planned_skill_ids: list[str],
    observed_skill_ids: list[str],
) -> bool:
    planned = {skill_id(item).strip() for item in planned_skill_ids if skill_id(item).strip()}
    observed = {skill_id(item).strip() for item in observed_skill_ids}
    return bool(planned) and planned.issubset(observed)


def _tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _group_by_request(
    records: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for index, record in enumerate(records):
        request_id = str(record.get("request_id") or f"__record_{index}")
        if groups and groups[-1][0] == request_id:
            groups[-1][1].append(record)
        else:
            groups.append((request_id, [record]))
    return groups


def _has_user_record(records: list[dict[str, Any]]) -> bool:
    return any(record.get("role") == "user" for record in records)


def _user_text(records: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(record.get("content") or "")
        for record in records
        if record.get("role") == "user"
    ).strip()


def _read_new_records(
    history_path: Path,
    session_state: dict[str, Any],
    *,
    completed_request_id: str,
    history_limit_bytes: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if history_path.suffix.lower() == ".jsonl":
        records, cursor = _read_new_jsonl_records(
            history_path,
            session_state,
            history_limit_bytes=history_limit_bytes,
        )
    else:
        all_records = load_history_records(history_path.parent.name)
        processed_count = max(
            0,
            int(session_state.get("processed_record_count") or 0),
        )
        if processed_count > len(all_records):
            processed_count = 0
        records = all_records[processed_count:]
        cursor = {
            "history_path": str(history_path),
            "processed_record_count": len(all_records),
            "history_offset": 0,
            "history_identity": "legacy_json",
        }

    selected, deferred, completed_ids = _select_completed_request_records(
        records,
        session_state=session_state,
        completed_request_id=completed_request_id,
    )
    cursor["deferred_records"] = deferred
    cursor["deferred_completed_request_ids"] = completed_ids
    return selected, cursor


def _bootstrap_activated_skill_state(
    session_id: str,
    history_path: Path,
    session_state: dict[str, Any],
) -> None:
    """Recover activations hidden behind a cursor written by older releases."""

    if session_state.get("activated_skill_state_initialized"):
        return
    processed_count = max(
        0,
        int(session_state.get("processed_record_count") or 0),
    )
    if processed_count:
        previous_identity = str(session_state.get("history_identity") or "")
        if history_path.suffix.lower() == ".jsonl" and previous_identity:
            try:
                stat = history_path.stat()
            except OSError:
                processed_count = 0
            else:
                if previous_identity != f"{stat.st_dev}:{stat.st_ino}":
                    processed_count = 0
        if processed_count:
            prior_records = load_history_records(session_id)[:processed_count]
            session_state["activated_skill_ids"] = _merge_skill_ids(
                _dedupe_skill_ids(session_state.get("activated_skill_ids") or []),
                _activated_skill_ids(prior_records),
            )[-_MAX_TRACKED_SKILLS:]
    session_state["activated_skill_state_initialized"] = True


def _read_new_jsonl_records(
    history_path: Path,
    session_state: dict[str, Any],
    *,
    history_limit_bytes: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        stat = history_path.stat()
    except OSError:
        return [], {
            "history_path": str(history_path),
            "processed_record_count": 0,
            "history_offset": 0,
            "history_identity": "",
        }
    identity = f"{stat.st_dev}:{stat.st_ino}"
    previous_identity = str(session_state.get("history_identity") or "")
    offset = max(0, int(session_state.get("history_offset") or 0))
    limit = stat.st_size
    if history_limit_bytes is not None:
        limit = min(limit, max(0, int(history_limit_bytes)))
    if previous_identity != identity or offset > limit:
        offset = 0
    payload = b""
    with history_path.open("rb") as handle:
        handle.seek(offset)
        payload = handle.read(limit - offset)
    records: list[dict[str, Any]] = []
    for raw_line in payload.decode("utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            item = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records, {
        "history_path": str(history_path),
        "processed_record_count": int(session_state.get("processed_record_count") or 0)
        + len(records),
        "history_offset": limit,
        "history_identity": identity,
    }


def _select_completed_request_records(
    new_records: list[dict[str, Any]],
    *,
    session_state: dict[str, Any],
    completed_request_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    existing = session_state.get("deferred_records")
    combined = [
        *(
            [dict(item) for item in existing if isinstance(item, dict)]
            if isinstance(existing, list)
            else []
        ),
        *new_records,
    ]
    if not completed_request_id:
        return combined, [], []

    known_completed = {
        str(item).strip()
        for item in session_state.get("deferred_completed_request_ids") or []
        if str(item).strip()
    }
    known_completed.add(str(completed_request_id).strip())
    groups: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(combined):
        request_id = str(record.get("request_id") or f"__record_{index}")
        group = groups.setdefault(
            request_id,
            {"first": index, "last": index, "order": index, "records": []},
        )
        group["last"] = index
        group["records"].append(record)
        if (
            record.get("event_type") == "chat.tool_result"
            and str(record.get("tool_name") or "") == "symphony_compose_graph"
        ):
            group["order"] = index
        if record.get("event_type") == SESSION_REQUEST_COMPLETED_EVENT:
            known_completed.add(request_id)

    target = groups.get(str(completed_request_id).strip())
    if target is not None:
        target_first = int(target["first"])
        for request_id, group in groups.items():
            if int(group["last"]) >= target_first:
                continue
            if _history_group_has_terminal_response(group["records"]):
                known_completed.add(request_id)

    ordered = sorted(groups.items(), key=lambda item: int(item[1]["order"]))
    first_incomplete = _first_incomplete_request_order(ordered, known_completed)
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    processed_ids: set[str] = set()
    for request_id, group in ordered:
        requires_completion = _history_group_requires_completion(group["records"])
        if request_id in known_completed and (
            first_incomplete is None or int(group["order"]) < first_incomplete
        ):
            selected.extend(group["records"])
            processed_ids.add(request_id)
        elif requires_completion:
            deferred.extend(group["records"])
    return selected, deferred, sorted((known_completed & groups.keys()) - processed_ids)


def _history_group_has_terminal_response(records: list[dict[str, Any]]) -> bool:
    for record in records:
        if record.get("event_type") in _TERMINAL_RESPONSE_EVENTS:
            return True
    return False


def _first_incomplete_request_order(
    ordered: list[tuple[str, dict[str, Any]]],
    known_completed: set[str],
) -> int | None:
    for request_id, group in ordered:
        if request_id in known_completed:
            continue
        if _history_group_requires_completion(group["records"]):
            return int(group["order"])
    return None


def _history_group_requires_completion(records: list[dict[str, Any]]) -> bool:
    return any(
        record.get("role") in {"user", "human"}
        and not record.get("is_goal_objective_message")
        for record in records
    ) or any(
        record.get("event_type") == "chat.tool_result"
        and str(record.get("tool_name") or "") == "symphony_compose_graph"
        for record in records
    )


def _read_state(graph_dir: Path) -> dict[str, Any]:
    path = session_feedback_state_path(graph_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("schema_version", SESSION_FEEDBACK_SCHEMA_VERSION)
    payload.setdefault("source", SESSION_FEEDBACK_SOURCE)
    payload.setdefault("sessions", {})
    payload.setdefault("stats", {})
    return payload


def _write_state(graph_dir: Path, state: dict[str, Any]) -> None:
    path = session_feedback_state_path(graph_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _update_state_stats(
    state: dict[str, Any],
    records: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    stats = state.setdefault("stats", {})
    stats["records_consumed"] = int(stats.get("records_consumed") or 0) + len(records)
    plans_observed = 0
    for record in records:
        if _is_symphony_plan_result(record):
            plans_observed += 1
    stats["plans_observed"] = (
        int(stats.get("plans_observed") or 0) + plans_observed
    )
    new_results = [item for item in results if not item.get("deduplicated")]
    stats["outcomes_recorded"] = int(stats.get("outcomes_recorded") or 0) + len(new_results)
    stats["duplicates_ignored"] = int(stats.get("duplicates_ignored") or 0) + sum(
        1 for item in results if item.get("deduplicated")
    )
    if results:
        state["last_result"] = results[-1]


def _is_symphony_plan_result(record: dict[str, Any]) -> bool:
    if record.get("event_type") != "chat.tool_result":
        return False
    if record.get("tool_name") != "symphony_compose_graph":
        return False
    raw_output = record.get("raw_output")
    if not isinstance(raw_output, dict):
        return False
    return bool(raw_output.get("plan_id"))


def _prune_sessions(sessions: dict[str, Any]) -> None:
    if len(sessions) <= _MAX_TRACKED_SESSIONS:
        return
    ordered = sorted(
        sessions.items(),
        key=lambda item: str(
            item[1].get("updated_at") if isinstance(item[1], dict) else ""
        ),
        reverse=True,
    )
    sessions.clear()
    sessions.update(ordered[:_MAX_TRACKED_SESSIONS])


def _graph_dir_matches(value: Any, graph_dir: Path) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    try:
        return Path(text).resolve() == graph_dir.resolve()
    except OSError:
        return False


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _on_consume_done(
    schedule_key: tuple[str, str, str],
    future: Future[dict[str, Any]],
) -> None:
    with _SCHEDULE_LOCK:
        _SCHEDULED.discard(schedule_key)
    try:
        result = future.result()
    except Exception:  # noqa: BLE001
        LOGGER.exception(
            "Symphony session feedback worker crashed: session_id=%s request_id=%s",
            schedule_key[1],
            schedule_key[2],
        )
        return
    if result.get("outcomes"):
        LOGGER.info(
            "Symphony session feedback recorded: session_id=%s request_id=%s outcomes=%s",
            schedule_key[1],
            schedule_key[2],
            len(result["outcomes"]),
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
