"""Read-only activity totals for the office activity tab (JM-16).

A Request counts as work only after an Execution has started and its outcome is
not ``ignored``. An ignored Request is neither a reply nor a failure. Token
totals count settled model calls only; unknown usage keeps the charged amount
already stored at settlement and is never treated as zero. The query does not
write, and it does not return settings, credentials or message text.
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


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _minutes(started: datetime, ended: datetime) -> int:
    return max(0, int((ended - started).total_seconds() // 60))


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
) -> dict[str, object]:
    """Fold stored rows into one member record per colleague. ``now`` must be timezone-aware."""
    if now.tzinfo is None:
        raise PersistentAgentError("activity clock must be timezone-aware", code="ACTIVITY_INVALID")
    moment = now.astimezone(timezone.utc)
    today = moment.date()
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
        day_minutes: dict[str, int] = defaultdict(int)
        day_calls: dict[str, int] = defaultdict(int)
        day_tokens: dict[str, int] = defaultdict(int)
        hour_minutes: dict[tuple[str, int], int] = defaultdict(int)
        hour_tasks: dict[tuple[str, int], int] = defaultdict(int)
        hour_tokens: dict[tuple[str, int], int] = defaultdict(int)
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
                    "name": mapped["platform_chat_id"] if mapped is not None else key,
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
            outcome = request["outcome"]
            ended = request["status"] == "ended" and request["updated_at"]
            spent = _minutes(started, _aware(str(request["updated_at"]))) if ended else 0
            tasks += 1
            if outcome == "completed":
                replies += 1
            elif outcome == "failed":
                failures += 1
            day = started.date().isoformat()
            day_tasks[day] += 1
            day_minutes[day] += spent
            hour_minutes[(day, started.hour)] += spent
            hour_tasks[(day, started.hour)] += 1
            if channel_name:
                bucket = channel_bucket(channel_name)
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
            started = starts[request_id]
            day = started.date().isoformat()
            day_calls[day] += 1
            day_tokens[day] += amount
            hour_tokens[(day, started.hour)] += amount
            session = by_session.get(str(request["session_id"]))
            if session is not None:
                bucket = channel_bucket(session["channel"])
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

        days = [
            {
                "date": day,
                "tasks": day_tasks[day],
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
                    "SELECT request_id, agent_id, session_id, status, outcome, updated_at "
                    "FROM requests WHERE namespace_id = ?",
                    (namespace_id,),
                ).fetchall()
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
                    "SELECT group_chat_id, platform, platform_chat_id FROM group_chats WHERE namespace_id = ?",
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
        )
