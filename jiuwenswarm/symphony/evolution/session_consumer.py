"""Learn Symphony runtime outcomes from durable session history."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.session.session_history import (
    get_read_history_path,
    load_history_records,
)
from jiuwenswarm.symphony.config import load_symphony_config
from jiuwenswarm.symphony.evolution.models import normalize_edges, skill_id
from jiuwenswarm.symphony.evolution.service import record_plan_outcome
from jiuwenswarm.symphony.score_storage import resolve_score_artifact_dir

LOGGER = logging.getLogger(__name__)

SESSION_FEEDBACK_SCHEMA_VERSION = "symphony.session_feedback.v1"
SESSION_FEEDBACK_STATE_FILE = "session_feedback_state.json"
SESSION_FEEDBACK_SOURCE = "session_history"
SESSION_INFERENCE_VERSION = "session_history_v1"
_MAX_TRACKED_SESSIONS = 500
_MAX_SKIPPED_TURNS = 3
_HISTORY_READY_RETRIES = 20
_HISTORY_READY_INTERVAL_SECONDS = 0.05
_SYMPHONY_TOOL_NAMES = {
    "symphony_compose_score",
    "symphony_read_score",
    "symphony_refresh_score",
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

_STATE_LOCK = threading.RLock()
_SCHEDULE_LOCK = threading.Lock()
_SCHEDULED: set[tuple[str, str, str]] = set()
_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="symphony-session-feedback",
)


def session_feedback_state_path(score_dir: str | Path) -> Path:
    return Path(score_dir) / "evolution" / SESSION_FEEDBACK_STATE_FILE


def schedule_session_evolution_consume(
    session_id: str,
    request_id: str,
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
        score_dir = resolve_score_artifact_dir(config.paths.score_dir)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("Symphony session feedback is unavailable: %s", exc)
        return False

    schedule_key = (str(score_dir), clean_session_id, clean_request_id)
    with _SCHEDULE_LOCK:
        if schedule_key in _SCHEDULED:
            return False
        _SCHEDULED.add(schedule_key)

    future = _EXECUTOR.submit(
        _consume_after_history_ready,
        clean_session_id,
        clean_request_id,
        score_dir,
    )
    future.add_done_callback(lambda current: _on_consume_done(schedule_key, current))
    return True


def _consume_after_history_ready(
    session_id: str,
    request_id: str,
    score_dir: Path,
) -> dict[str, Any]:
    history_path = get_read_history_path(session_id)
    history_limit = _wait_for_request_history(session_id, request_id, history_path)
    return consume_session_history(
        session_id,
        completed_request_id=request_id,
        score_dir=score_dir,
        history_limit_bytes=history_limit,
    )


def _wait_for_request_history(
    session_id: str,
    request_id: str,
    history_path: Path,
) -> int | None:
    """Wait off the request thread until this request's terminal record is durable."""

    for _attempt in range(_HISTORY_READY_RETRIES):
        records = load_history_records(session_id)
        if any(
            str(record.get("request_id") or "") == request_id
            and record.get("event_type") == "chat.final"
            for record in reversed(records)
        ):
            return _file_size(history_path)
        time.sleep(_HISTORY_READY_INTERVAL_SECONDS)
    return _file_size(history_path)


def consume_session_history(
    session_id: str,
    *,
    completed_request_id: str = "",
    score_dir: str | Path | None = None,
    history_limit_bytes: int | None = None,
) -> dict[str, Any]:
    """Incrementally consume one session and emit at most one outcome per plan."""

    clean_session_id = str(session_id or "").strip()
    if not clean_session_id:
        return {"success": False, "detail": "session_id is required"}
    if score_dir is None:
        config = load_symphony_config()
        if not config.enabled or not config.evolution.enabled:
            return {
                "success": True,
                "enabled": False,
                "recorded": False,
                "source": SESSION_FEEDBACK_SOURCE,
            }
        score_dir = resolve_score_artifact_dir(config.paths.score_dir)
    resolved_score_dir = Path(score_dir).resolve()
    history_path = get_read_history_path(clean_session_id)

    with _STATE_LOCK:
        state = _read_state(resolved_score_dir)
        sessions = state.setdefault("sessions", {})
        session_state = dict(sessions.get(clean_session_id) or {})
        try:
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
                score_dir=resolved_score_dir,
            )
            session_state.update(cursor)
            session_state["updated_at"] = _utc_now()
            sessions[clean_session_id] = session_state
            _update_state_stats(state, records, results)
            state["last_error"] = ""
            state["updated_at"] = _utc_now()
            _prune_sessions(sessions)
            _write_state(resolved_score_dir, state)
        except Exception as exc:  # noqa: BLE001
            state["last_error"] = str(exc)[:1000]
            state["updated_at"] = _utc_now()
            _write_state(resolved_score_dir, state)
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


def session_feedback_status(score_dir: str | Path) -> dict[str, Any]:
    """Return frontend-safe observability for the session feedback worker."""

    resolved_score_dir = Path(score_dir).resolve()
    with _STATE_LOCK:
        state = _read_state(resolved_score_dir)
    sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    stats = state.get("stats") if isinstance(state.get("stats"), dict) else {}
    with _SCHEDULE_LOCK:
        pending_jobs = sum(
            1 for item in _SCHEDULED if item[0] == str(resolved_score_dir)
        )
    return {
        "available": True,
        "source": SESSION_FEEDBACK_SOURCE,
        "mode": "incremental_async",
        "state_path": str(session_feedback_state_path(resolved_score_dir)),
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
    score_dir: Path,
) -> list[dict[str, Any]]:
    pending = (
        dict(session_state.get("pending_plan") or {})
        if isinstance(session_state.get("pending_plan"), dict)
        else {}
    )
    results: list[dict[str, Any]] = []
    for request_id, request_records in _group_by_request(records):
        markers = _plan_markers(request_records, score_dir=score_dir)

        if markers:
            # A new plan supersedes an older plan that the user never executed.
            pending = {}
        elif pending:
            result = _consume_execution_turn(
                pending,
                request_records,
                session_id=session_id,
                request_id=request_id,
                score_dir=score_dir,
                same_turn=False,
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
                score_dir=score_dir,
                same_turn=True,
            )
            if result is not None:
                results.append(result)
                pending = {}

    if pending:
        session_state["pending_plan"] = pending
    else:
        session_state.pop("pending_plan", None)
    return results


def _consume_execution_turn(
    pending: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    session_id: str,
    request_id: str,
    score_dir: Path,
    same_turn: bool,
) -> dict[str, Any] | None:
    correlation = _execution_correlation(pending, records, same_turn=same_turn)
    if not correlation:
        return None
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
    planned_skill_ids = list(pending.get("selected_skill_ids") or [])
    if outcome == "success" and not _all_planned_skills_observed(
        planned_skill_ids,
        observed_skills,
    ):
        return None
    planned_edges = normalize_edges(pending.get("selected_edges") or [])
    selected_edges = _observed_edges(planned_edges, observed_skills)
    failed_edges = selected_edges[-1:] if outcome == "failure" else []
    evidence_id = f"session:{session_id}:{pending['plan_id']}:{request_id}"
    event = record_plan_outcome(
        score_dir,
        plan_id=str(pending["plan_id"]),
        query=str(pending.get("query") or ""),
        outcome=outcome,
        selected_skill_ids=observed_skills,
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
    score_dir: Path,
) -> list[tuple[int, dict[str, Any]]]:
    output: list[tuple[int, dict[str, Any]]] = []
    for index, record in enumerate(records):
        if record.get("event_type") != "chat.tool_result":
            continue
        if str(record.get("tool_name") or "") != "symphony_compose_score":
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
        if not plan_id or not _score_dir_matches(raw_output.get("score_dir"), score_dir):
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
) -> str:
    if not records:
        return ""
    if _observed_skill_ids(records, pending.get("selected_skill_ids") or []):
        return "planned_skill_observed"
    observed_tools = _observed_tool_names(records)
    has_execution_tool = any(
        name and name not in _SYMPHONY_TOOL_NAMES
        for name in observed_tools
    )
    if same_turn:
        return "same_turn_tool_execution" if has_execution_tool else ""
    if not _CONTINUATION_RE.search(_user_text(records)):
        return ""
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
        return "success", "", final_text[-1000:]
    return None


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
        for key in ("data", "raw_output", "rawOutput", "result"):
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
        return _read_new_jsonl_records(
            history_path,
            session_state,
            history_limit_bytes=history_limit_bytes,
        )

    records = load_history_records(history_path.parent.name)
    processed_count = max(0, int(session_state.get("processed_record_count") or 0))
    if processed_count > len(records):
        processed_count = 0
    limit = len(records)
    if completed_request_id:
        matching = [
            index
            for index, record in enumerate(records)
            if str(record.get("request_id") or "") == completed_request_id
        ]
        if matching:
            limit = matching[-1] + 1
    return records[processed_count:limit], {
        "history_path": str(history_path),
        "processed_record_count": limit,
        "history_offset": 0,
        "history_identity": "legacy_json",
    }


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


def _read_state(score_dir: Path) -> dict[str, Any]:
    path = session_feedback_state_path(score_dir)
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


def _write_state(score_dir: Path, state: dict[str, Any]) -> None:
    path = session_feedback_state_path(score_dir)
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
    if record.get("tool_name") != "symphony_compose_score":
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


def _score_dir_matches(value: Any, score_dir: Path) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    try:
        return Path(text).resolve() == score_dir.resolve()
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
