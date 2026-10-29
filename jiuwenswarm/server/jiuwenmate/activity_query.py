"""Read-only activity totals for the office activity tab (JM-16).

A Request counts as work only after an Execution has started and its outcome is
not ``ignored``. An ignored Request is neither a reply nor a failure. Token
totals count settled model calls only; unknown usage keeps the charged amount
already stored at settlement and is never treated as zero. The query does not
write, and it does not return settings, credentials or full message bodies.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError, require_identifier
from jiuwenswarm.server.jiuwenmate.output_files import FILE_SHARE_MODES

_FILE_DELIVERED = frozenset({"delivered", "available"})
_HISTORY_LIMIT = 200
_CHANNEL_KINDS = frozenset({"group", "im_direct"})
_HISTORY_TITLE_LIMIT = 120


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _minutes(started: datetime, ended: datetime) -> int:
    return max(0, int((ended - started).total_seconds() // 60))


def _group_label(mapped: Mapping[str, str] | None, key: str) -> str:
    """Prefer the stored group name. A platform chat id is only a fallback."""
    if mapped is None:
        return key
    stored = _optional(mapped, "name")
    if stored and stored.strip():
        return stored.strip()
    return str(mapped["platform_chat_id"])


def _optional(row: Mapping[str, str | None], key: str) -> str | None:
    try:
        value = row[key]
    except (KeyError, IndexError):
        return None
    return None if value is None else str(value)


def _plan_id(source_ref: str) -> str:
    parts = source_ref.split(":", 2)
    return parts[1] if len(parts) >= 2 and parts[0] == "schedule" else ""


def _request_title(request: Mapping[str, str | None]) -> str:
    """Return a compact title from a private web request, if one is available."""
    record_json = _optional(request, "record_json")
    if not record_json:
        return ""
    try:
        record = json.loads(record_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    request_input = record.get("request_input") if isinstance(record, Mapping) else None
    if not isinstance(request_input, Mapping) or request_input.get("kind") != "direct_message":
        return ""
    text = request_input.get("text")
    if not isinstance(text, str):
        return ""
    title = " ".join(text.split())
    if len(title) > _HISTORY_TITLE_LIMIT:
        return title[: _HISTORY_TITLE_LIMIT - 1].rstrip() + "…"
    return title


def _work_kind(source_ref: str, session: Mapping[str, str] | None) -> str:
    if source_ref.startswith("schedule:"):
        return "schedule"
    if session is not None and session["kind"] in _CHANNEL_KINDS:
        return "channel"
    return "conversation"


def _transport_channel(
    session: Mapping[str, str] | None,
    group_names: Mapping[str, Mapping[str, str]],
) -> str:
    """Return an IM platform for channel metrics; group and Web sessions are not platforms."""
    if session is None:
        return ""
    if session["kind"] == "im_direct":
        return session["channel"]
    if session["kind"] == "group":
        mapped = group_names.get(session["conversation_key"])
        return "" if mapped is None else _optional(mapped, "platform") or ""
    return ""


def _history_title(
    source_ref: str,
    session: Mapping[str, str] | None,
    *,
    conversation_titles: Mapping[tuple[str, str], str],
    plan_names: Mapping[str, str],
    group_names: Mapping[str, Mapping[str, str]],
) -> str:
    plan = _plan_id(source_ref)
    if plan:
        return plan_names.get(plan, "")
    if session is None:
        return ""
    if session["kind"] == "group":
        mapped = group_names.get(session["conversation_key"])
        return str(mapped["platform_chat_id"]) if mapped is not None else ""
    return conversation_titles.get((session["agent_id"], session["conversation_key"]), "")


def assemble_activity(
    *,
    colleagues: Sequence[Mapping[str, str]],
    requests: Sequence[Mapping[str, str | None]],
    executions: Sequence[Mapping[str, str]],
    sessions: Sequence[Mapping[str, str]],
    calls: Sequence[Mapping[str, object]],
    deliveries: Sequence[Mapping[str, str]],
    groups: Sequence[Mapping[str, str]],
    now: datetime,
    conversation_titles: Mapping[tuple[str, str], str] | None = None,
    plan_names: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Fold stored rows into one member record per colleague. ``now`` must be timezone-aware."""
    if now.tzinfo is None:
        raise PersistentAgentError("activity clock must be timezone-aware", code="ACTIVITY_INVALID")
    moment = now.astimezone(timezone.utc)
    today = moment.date()
    titles = {} if conversation_titles is None else conversation_titles
    plans = {} if plan_names is None else plan_names
    by_session = {row["session_id"]: row for row in sessions}
    starts: dict[str, datetime] = {}
    execution_request: dict[str, str] = {}
    for row in executions:
        execution_request[row["execution_id"]] = row["request_id"]
        started = _aware(row["started_at"])
        current = starts.get(row["request_id"])
        if current is None or started < current:
            starts[row["request_id"]] = started
    request_by_id = {str(row["request_id"]): row for row in requests}
    group_names = {row["group_chat_id"]: row for row in groups}
    members: list[dict[str, object]] = []
    for colleague in colleagues:
        agent_id = colleague["agent_id"]
        created = _aware(colleague["created_at"]).date()
        companion_days = max(1, (today - created).days + 1)
        tasks = replies = failures = calls_total = tokens_total = 0
        last_response: datetime | None = None
        day_tasks: dict[str, int] = defaultdict(int)
        day_finished_tasks: dict[str, int] = defaultdict(int)
        day_failures: dict[str, int] = defaultdict(int)
        day_minutes: dict[str, int] = defaultdict(int)
        day_calls: dict[str, int] = defaultdict(int)
        day_tokens: dict[str, int] = defaultdict(int)
        hour_minutes: dict[tuple[str, int], int] = defaultdict(int)
        hour_tasks: dict[tuple[str, int], int] = defaultdict(int)
        hour_tokens: dict[tuple[str, int], int] = defaultdict(int)
        request_tokens: dict[str, int] = defaultdict(int)
        day_kinds: dict[str, dict[str, int]] = defaultdict(lambda: {"conversation": 0, "channel": 0, "schedule": 0})
        history_rows: list[dict[str, object]] = []
        running_task_started_ats: list[str] = []
        channels: dict[str, dict[str, int]] = {}
        group_rows: dict[str, dict[str, object]] = {}

        def channel_bucket(name: str) -> dict[str, int]:
            bucket = channels.get(name)
            if bucket is None:
                bucket = {"calls": 0, "tokens": 0, "tasks": 0, "replies": 0, "failures": 0}
                channels[name] = bucket
            return bucket

        def group_bucket(session: Mapping[str, str]) -> dict[str, object]:
            key = session["conversation_key"]
            bucket = group_rows.get(key)
            if bucket is None:
                mapped = group_names.get(key)
                bucket = {
                    "group_chat_id": key,
                    "name": _group_label(mapped, key),
                    "platform": mapped["platform"] if mapped is not None else "",
                    "artifacts": 0,
                    "tokens": 0,
                    "tasks": 0,
                    "last_response_at": None,
                }
                group_rows[key] = bucket
            return bucket

        own_requests = [row for row in requests if row["agent_id"] == agent_id]
        for request in own_requests:
            request_id = str(request["request_id"])
            started = starts.get(request_id)
            if started is None or request["outcome"] == "ignored":
                continue
            session = by_session.get(str(request["session_id"]))
            channel_name = session["channel"] if session is not None else ""
            transport_channel = _transport_channel(session, group_names)
            outcome = request["outcome"]
            if request["status"] == "dispatched" and outcome is None:
                running_task_started_ats.append(started.isoformat())
            ended = request["status"] == "ended" and request["updated_at"]
            spent = _minutes(started, _aware(str(request["updated_at"]))) if ended else 0
            tasks += 1
            if ended:
                day_finished_tasks[started.date().isoformat()] += 1
            if outcome == "completed":
                replies += 1
            elif outcome == "failed":
                failures += 1
                day_failures[started.date().isoformat()] += 1
            day = started.date().isoformat()
            day_tasks[day] += 1
            day_minutes[day] += spent
            hour_minutes[(day, started.hour)] += spent
            hour_tasks[(day, started.hour)] += 1
            source_ref = _optional(request, "source_ref") or ""
            kind = _work_kind(source_ref, session)
            title = _history_title(
                source_ref,
                session,
                conversation_titles=titles,
                plan_names=plans,
                group_names=group_names,
            )
            if source_ref.startswith("web:") and session is not None and session["kind"] == "web_direct":
                title = _request_title(request) or title
            day_kinds[day][kind] += 1
            updated_at = _optional(request, "updated_at")
            history_rows.append(
                {
                    "request_id": request_id,
                    "title": title,
                    "kind": kind,
                    "channel": channel_name,
                    "status": str(outcome or request["status"] or ""),
                    "updated_at": updated_at,
                    "started_at": started.isoformat(),
                }
            )
            if transport_channel:
                bucket = channel_bucket(transport_channel)
                bucket["tasks"] += 1
                if outcome == "completed":
                    bucket["replies"] += 1
                elif outcome == "failed":
                    bucket["failures"] += 1
            if session is not None and session["kind"] == "group":
                group = group_bucket(session)
                group["tasks"] = int(group["tasks"]) + 1
            if outcome == "completed" and request["updated_at"]:
                updated = _aware(str(request["updated_at"]))
                if last_response is None or updated > last_response:
                    last_response = updated
                if session is not None and session["kind"] == "group":
                    group = group_bucket(session)
                    previous = group["last_response_at"]
                    if not isinstance(previous, str) or updated.isoformat() > previous:
                        group["last_response_at"] = updated.isoformat()

        for call in calls:
            if call["status"] != "settled":
                continue
            ref = call["ref"]
            execution_id = ref.get("execution_id") if isinstance(ref, Mapping) else None
            request_id = execution_request.get(execution_id) if isinstance(execution_id, str) else None
            request = request_by_id.get(request_id or "")
            if request is None or request["agent_id"] != agent_id or request["outcome"] == "ignored":
                continue
            if request_id not in starts:
                continue
            charged = call["charged_tokens"]
            amount = charged if isinstance(charged, int) and not isinstance(charged, bool) else 0
            calls_total += 1
            tokens_total += amount
            request_tokens[request_id] += amount
            started = starts[request_id]
            day = started.date().isoformat()
            day_calls[day] += 1
            day_tokens[day] += amount
            hour_tokens[(day, started.hour)] += amount
            session = by_session.get(str(request["session_id"]))
            if session is not None:
                transport_channel = _transport_channel(session, group_names)
                if transport_channel:
                    bucket = channel_bucket(transport_channel)
                    bucket["calls"] += 1
                    bucket["tokens"] += amount
                if session["kind"] == "group":
                    group = group_bucket(session)
                    group["tokens"] = int(group["tokens"]) + amount

        for delivery in deliveries:
            if delivery["status"] not in _FILE_DELIVERED or delivery["share_mode"] not in FILE_SHARE_MODES:
                continue
            request = request_by_id.get(delivery["request_id"])
            if request is None or request["agent_id"] != agent_id or request["outcome"] == "ignored":
                continue
            session = by_session.get(str(request["session_id"]))
            if session is None or session["kind"] != "group":
                continue
            group = group_bucket(session)
            group["artifacts"] = int(group["artifacts"]) + 1

        for item in history_rows:
            item["tokens"] = request_tokens.get(str(item["request_id"]), 0)
        history_rows.sort(key=lambda item: str(item["updated_at"] or item["started_at"]), reverse=True)
        days = [
            {
                "date": day,
                "tasks": day_tasks[day],
                "finished_tasks": day_finished_tasks[day],
                "failures": day_failures[day],
                "minutes": day_minutes[day],
                "calls": day_calls[day],
                "tokens": day_tokens[day],
            }
            for day in sorted(set(day_tasks) | set(day_calls))
        ]
        hours = [
            {
                "date": day,
                "hour": hour,
                "minutes": hour_minutes[(day, hour)],
                "tasks": hour_tasks[(day, hour)],
                "tokens": hour_tokens[(day, hour)],
            }
            for day, hour in sorted(set(hour_minutes) | set(hour_tasks) | set(hour_tokens))
            if hour_tasks[(day, hour)] or hour_tokens[(day, hour)]
        ]
        members.append(
            {
                "agent_id": agent_id,
                "calls": calls_total,
                "tokens": tokens_total,
                "tasks": tasks,
                "replies": replies,
                "failures": failures,
                "companion_days": companion_days,
                "last_response_at": None if last_response is None else last_response.isoformat(),
                "days": days,
                "kinds": [{"date": day, **day_kinds[day]} for day in sorted(day_kinds)],
                "history": history_rows[:_HISTORY_LIMIT],
                "running_task_started_ats": running_task_started_ats,
                "hours": hours,
                "channels": [
                    {"channel": name, **bucket}
                    for name, bucket in sorted(channels.items())
                ],
                "groups": [group_rows[key] for key in sorted(group_rows)],
            }
        )
    return {"generated_at": moment.isoformat(), "members": members}


class ActivityQuery:
    """Reads the shared JiuwenMate database and returns :func:`assemble_activity`."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    def report(self, namespace_id: str, *, now: datetime) -> dict[str, object]:
        require_identifier("namespace_id", namespace_id)
        try:
            with closing(sqlite3.connect(self._db_path, timeout=30)) as conn:
                conn.row_factory = sqlite3.Row
                colleagues = conn.execute(
                    "SELECT agent_id, created_at FROM colleagues WHERE namespace_id = ? ORDER BY created_at, agent_id",
                    (namespace_id,),
                ).fetchall()
                requests = conn.execute(
                    "SELECT request_id, agent_id, session_id, source_ref, status, outcome, record_json, updated_at "
                    "FROM requests WHERE namespace_id = ?",
                    (namespace_id,),
                ).fetchall()
                conversation_titles = {
                    (row["agent_id"], row["conversation_id"]): str(row["title"] or "")
                    for row in conn.execute(
                        "SELECT agent_id, conversation_id, title FROM web_conversations "
                        "WHERE namespace_id = ? AND deleted_at IS NULL",
                        (namespace_id,),
                    )
                }
                plan_names = {
                    row["plan_id"]: str(row["name"] or "")
                    for row in conn.execute(
                        "SELECT plan_id, name FROM scheduled_plans WHERE namespace_id = ?",
                        (namespace_id,),
                    )
                }
                executions = conn.execute(
                    "SELECT execution_id, request_id, started_at FROM executions WHERE namespace_id = ?",
                    (namespace_id,),
                ).fetchall()
                sessions = conn.execute(
                    "SELECT session_id, agent_id, kind, channel, conversation_key FROM sessions WHERE namespace_id = ?",
                    (namespace_id,),
                ).fetchall()
                calls = conn.execute(
                    "SELECT status, charged_tokens, ref_json FROM budget_calls WHERE namespace_id = ?",
                    (namespace_id,),
                ).fetchall()
                deliveries = conn.execute(
                    "SELECT d.request_id, d.status, o.share_mode FROM result_deliveries d "
                    "JOIN candidate_outputs o ON o.namespace_id = d.namespace_id AND o.output_id = d.output_id "
                    "WHERE d.namespace_id = ?",
                    (namespace_id,),
                ).fetchall()
                groups = conn.execute(
                    "SELECT g.group_chat_id, g.platform, g.platform_chat_id, d.name AS name "
                    "FROM group_chats g LEFT JOIN group_details d "
                    "ON d.namespace_id = g.namespace_id AND d.group_chat_id = g.group_chat_id "
                    "WHERE g.namespace_id = ?",
                    (namespace_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise PersistentAgentError("activity records are unavailable", code="ACTIVITY_UNAVAILABLE") from exc
        parsed_calls = []
        for row in calls:
            try:
                ref = json.loads(row["ref_json"])
            except json.JSONDecodeError:
                ref = {}
            parsed_calls.append(
                {"status": row["status"], "charged_tokens": row["charged_tokens"], "ref": ref if isinstance(ref, dict) else {}}
            )
        return assemble_activity(
            colleagues=colleagues,
            requests=requests,
            executions=executions,
            sessions=sessions,
            calls=parsed_calls,
            deliveries=deliveries,
            groups=groups,
            now=now,
            conversation_titles=conversation_titles,
            plan_names=plan_names,
        )
