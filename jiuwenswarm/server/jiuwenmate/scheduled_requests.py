"""Plans and due triggers for JiuwenMate colleagues (JM-19).

A plan lives in the colleague database, not in the personal-edition cron store.
Time arithmetic reuses the shared cron expression helper. A due trigger becomes
one ordinary Request in that colleague's Web chat, so the reminder shows in the
same window as a private message. The same occurrence is identified by
``schedule:{plan_id}:{scheduled_for}`` and is accepted only once.
"""

from __future__ import annotations

import asyncio
import uuid
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jiuwenswarm.runtime.cron.cron_expr import next_cron_datetime, validate_cron_expression
from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot, ConversationRef, RequestRouteSnapshot
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError, require_identifier
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import WORKSPACE_READ, WORKSPACE_WRITE
from jiuwenswarm.server.jiuwenmate.session_scheduler import SessionScheduler
from jiuwenswarm.server.jiuwenmate.spec_provider import ConfigSnapshot
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore
from jiuwenswarm.server.jiuwenmate.web_conversations import WebConversationStore
from openjiuwen.harness import persistent_agent as core

CATCH_UP_LIMIT = 48
DEFAULT_TIMEZONE = "Asia/Shanghai"
_NAME_LIMIT = 80
_PROMPT_LIMIT = 4000

SCHEDULE_ACCESS = AccessSnapshot(
    principal_ref="local",
    startup_scope=frozenset({"scheduled", WORKSPACE_READ, WORKSPACE_WRITE}),
    source_scope=frozenset({"scheduled", WORKSPACE_READ, WORKSPACE_WRITE}),
    policy_revision="local",
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise PersistentAgentError("schedule clock must be timezone-aware", code="SCHEDULE_INVALID")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def occurrence_after(cron_expr: str, moment: datetime, timezone_name: str) -> datetime:
    """The first occurrence strictly after ``moment``, stored as UTC."""
    try:
        zone = ZoneInfo(timezone_name)
        upcoming = next_cron_datetime(cron_expr, _utc(moment).astimezone(zone))
    except (ValueError, KeyError, OverflowError) as exc:
        raise PersistentAgentError("invalid schedule", code="SCHEDULE_INVALID") from exc
    return _utc(upcoming)


def employee_conversation_id(agent_id: str) -> str:
    """Chat created for reminders when the colleague has no listed Web conversation yet."""
    return f"employee:{agent_id}"


def source_of(plan_id: str, scheduled_for: datetime) -> str:
    return f"schedule:{plan_id}:{_stamp(scheduled_for)}"


@dataclass(frozen=True, kw_only=True)
class ScheduledPlan:
    """A colleague's enabled or paused plan and the next occurrence still owed."""

    namespace_id: str
    plan_id: str
    agent_id: str
    name: str
    prompt: str
    cron_expr: str
    timezone_name: str
    enabled: bool
    next_fire_at: datetime | None
    created_at: datetime

    schedule_kind: str = "cron"
    interval_seconds: int | None = None
    run_at: datetime | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    run_count: int = 0
    last_status: str = "running"
    chat_conversation_id: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "agent_id": self.agent_id,
            "name": self.name,
            "prompt": self.prompt,
            "cron_expr": self.cron_expr,
            "timezone": self.timezone_name,
            "enabled": self.enabled,
            "next_fire_at": _stamp(self.next_fire_at) if self.next_fire_at else None,
            "schedule_kind": self.schedule_kind,
            "interval_seconds": self.interval_seconds,
            "run_at": _stamp(self.run_at) if self.run_at else None,
            "starts_at": _stamp(self.starts_at) if self.starts_at else None,
            "ends_at": _stamp(self.ends_at) if self.ends_at else None,
            "conversation_id": self.chat_conversation_id or employee_conversation_id(self.agent_id),
            "run_count": self.run_count,
            "last_status": self.last_status,
            "created_at": _stamp(self.created_at),
        }


class ScheduleStore(SQLiteStore):
    """Plans and the occurrence each one has already turned into a Request."""

    _schema = """
    CREATE TABLE IF NOT EXISTS scheduled_plans (
        namespace_id TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        name TEXT NOT NULL,
        prompt TEXT NOT NULL,
        cron_expr TEXT NOT NULL,
        timezone_name TEXT NOT NULL,
        enabled INTEGER NOT NULL,
        next_fire_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (namespace_id, plan_id)
    );
    CREATE TABLE IF NOT EXISTS scheduled_fires (
        namespace_id TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        scheduled_for TEXT NOT NULL,
        request_id TEXT NOT NULL,
        PRIMARY KEY (namespace_id, plan_id, scheduled_for)
    );
    """

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        with self._transaction() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(scheduled_plans)")}
            for name, declaration in (
                ("schedule_kind", "TEXT NOT NULL DEFAULT 'cron'"),
                ("interval_seconds", "INTEGER"), ("run_at", "TEXT"),
                ("starts_at", "TEXT"), ("ends_at", "TEXT"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE scheduled_plans ADD COLUMN {name} {declaration}")
            conn.execute("CREATE TABLE IF NOT EXISTS scheduled_manual_fires (namespace_id TEXT, plan_id TEXT, "
                         "scheduled_for TEXT, request_id TEXT PRIMARY KEY)")

    def insert(self, plan: ScheduledPlan) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_plans (
                    namespace_id, plan_id, agent_id, name, prompt, cron_expr, timezone_name,
                    enabled, next_fire_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.namespace_id,
                    plan.plan_id,
                    plan.agent_id,
                    plan.name,
                    plan.prompt,
                    plan.cron_expr,
                    plan.timezone_name,
                    int(plan.enabled),
                    _stamp(plan.next_fire_at) if plan.next_fire_at else "",
                    _stamp(plan.created_at),
                ),
            )

    def save(self, plan: ScheduledPlan) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE scheduled_plans SET name=?, prompt=?, cron_expr=?, timezone_name=?, enabled=?, "
                "next_fire_at=?, schedule_kind=?, interval_seconds=?, run_at=?, starts_at=?, ends_at=? "
                "WHERE namespace_id=? AND plan_id=?",
                (plan.name, plan.prompt, plan.cron_expr, plan.timezone_name, int(plan.enabled),
                 _stamp(plan.next_fire_at) if plan.next_fire_at else "", plan.schedule_kind, plan.interval_seconds,
                 _stamp(plan.run_at) if plan.run_at else None, _stamp(plan.starts_at) if plan.starts_at else None,
                 _stamp(plan.ends_at) if plan.ends_at else None, plan.namespace_id, plan.plan_id),
            )

    def fires(self, namespace_id: str, plan_id: str) -> list:
        with self._transaction() as conn:
            return conn.execute(
                "SELECT scheduled_for, request_id FROM scheduled_fires WHERE namespace_id=? AND plan_id=? "
                "UNION ALL SELECT scheduled_for, request_id FROM scheduled_manual_fires "
                "WHERE namespace_id=? AND plan_id=? ORDER BY scheduled_for DESC",
                (namespace_id, plan_id, namespace_id, plan_id),
            ).fetchall()

    def list(self, namespace_id: str, agent_id: str | None = None) -> list[ScheduledPlan]:
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scheduled_plans
                WHERE namespace_id = ? AND (? IS NULL OR agent_id = ?)
                ORDER BY created_at, plan_id
                """,
                (namespace_id, agent_id, agent_id),
            ).fetchall()
        return [_plan(row) for row in rows]

    def get(self, namespace_id: str, plan_id: str) -> ScheduledPlan:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM scheduled_plans WHERE namespace_id = ? AND plan_id = ?",
                (namespace_id, plan_id),
            ).fetchone()
        if row is None:
            raise PersistentAgentError("schedule not found", code="NOT_FOUND")
        return _plan(row)

    def due(self, now: datetime) -> list[ScheduledPlan]:
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scheduled_plans
                WHERE enabled = 1 AND next_fire_at != '' AND next_fire_at <= ?
                ORDER BY next_fire_at, plan_id
                """,
                (_stamp(now),),
            ).fetchall()
        return [_plan(row) for row in rows]

    def save_next(self, namespace_id: str, plan_id: str, next_fire_at: datetime | None, *, enabled: bool) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE scheduled_plans SET next_fire_at = ?, enabled = ?
                WHERE namespace_id = ? AND plan_id = ?
                """,
                (_stamp(next_fire_at) if next_fire_at else "", int(enabled), namespace_id, plan_id),
            )

    def fired(self, namespace_id: str, plan_id: str, scheduled_for: datetime) -> str | None:
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT request_id FROM scheduled_fires
                WHERE namespace_id = ? AND plan_id = ? AND scheduled_for = ?
                """,
                (namespace_id, plan_id, _stamp(scheduled_for)),
            ).fetchone()
        return None if row is None else str(row["request_id"])

    def record_fire(self, namespace_id: str, plan_id: str, scheduled_for: datetime, request_id: str, *, manual: bool = False) -> None:
        table = "scheduled_manual_fires" if manual else "scheduled_fires"
        with self._transaction() as conn:
            conn.execute(
                f"""
                INSERT OR IGNORE INTO {table} (namespace_id, plan_id, scheduled_for, request_id)
                VALUES (?, ?, ?, ?)
                """,
                (namespace_id, plan_id, _stamp(scheduled_for), request_id),
            )

    def delete(self, namespace_id: str, plan_id: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM scheduled_fires WHERE namespace_id = ? AND plan_id = ?",
                (namespace_id, plan_id),
            )
            deleted = conn.execute(
                "DELETE FROM scheduled_plans WHERE namespace_id = ? AND plan_id = ?",
                (namespace_id, plan_id),
            ).rowcount
        if deleted == 0:
            raise PersistentAgentError("schedule not found", code="NOT_FOUND")

    def delete_agent(self, namespace_id: str, agent_id: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                DELETE FROM scheduled_fires WHERE namespace_id = ? AND plan_id IN (
                    SELECT plan_id FROM scheduled_plans WHERE namespace_id = ? AND agent_id = ?
                )
                """,
                (namespace_id, namespace_id, agent_id),
            )
            conn.execute(
                "DELETE FROM scheduled_plans WHERE namespace_id = ? AND agent_id = ?",
                (namespace_id, agent_id),
            )


def _plan(row) -> ScheduledPlan:
    return ScheduledPlan(
        namespace_id=row["namespace_id"],
        plan_id=row["plan_id"],
        agent_id=row["agent_id"],
        name=row["name"],
        prompt=row["prompt"],
        cron_expr=row["cron_expr"],
        timezone_name=row["timezone_name"],
        enabled=bool(row["enabled"]),
        next_fire_at=_parse(row["next_fire_at"]) if row["next_fire_at"] else None,
        schedule_kind=row["schedule_kind"], interval_seconds=row["interval_seconds"],
        run_at=_parse(row["run_at"]) if row["run_at"] else None,
        starts_at=_parse(row["starts_at"]) if row["starts_at"] else None,
        ends_at=_parse(row["ends_at"]) if row["ends_at"] else None,
        created_at=_parse(row["created_at"]),
    )


class ScheduleService:
    """Creates plans and turns each owed occurrence into one scheduled Request."""

    def __init__(
        self,
        sessions: ExecutionRepository,
        scheduler: SessionScheduler,
        snapshots: Callable[[str, str], ConfigSnapshot],
        colleagues: Callable[[str, str], object],
        *,
        clock: Callable[[], datetime],
        store: ScheduleStore | None = None,
        ids: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._sessions = sessions
        self._scheduler = scheduler
        self._snapshots = snapshots
        self._colleagues = colleagues
        self._clock = clock
        self._store = store or ScheduleStore(sessions.db_path)
        self._chats = WebConversationStore(sessions.db_path)
        self._ids = ids
        self._guard = threading.RLock()
        self._busy: set[tuple[str, str]] = set()

    @contextmanager
    def _claim(self, namespace_id: str, plan_id: str):
        key = (namespace_id, plan_id)
        with self._guard:
            if key in self._busy:
                raise PersistentAgentError("schedule is being updated or triggered; retry", code="CONFLICT")
            self._busy.add(key)
        try:
            yield
        finally:
            with self._guard:
                self._busy.remove(key)

    def _describe(self, plan: ScheduledPlan) -> ScheduledPlan:
        runs = self._runs(plan)
        status = "running" if plan.enabled else "paused"
        if plan.next_fire_at is None or (plan.ends_at and plan.ends_at < _utc(self._clock())):
            status = "completed"
        if runs and runs[0]["status"] in {"queued", "running"}:
            status = "running"
        elif runs and runs[0]["status"] == "failed":
            status = "failed"
        return replace(
            plan,
            run_count=len(runs),
            last_status=status,
            chat_conversation_id=self._window_id(plan.namespace_id, plan.agent_id),
        )

    def _window_id(self, namespace_id: str, agent_id: str) -> str:
        """The colleague's current chat: a pinned one, otherwise the latest, otherwise a stable new id."""
        listed = self._chats.list(namespace_id, agent_id)
        if listed:
            return listed[0].conversation_id
        return employee_conversation_id(agent_id)

    def _open_chat(self, plan: ScheduledPlan, now: datetime) -> ConversationRef:
        """List the colleague's chat and return it so the reminder is visible in that window."""
        conversation_id = self._window_id(plan.namespace_id, plan.agent_id)
        self._chats.touch(
            plan.namespace_id,
            plan.agent_id,
            conversation_id,
            text=plan.name,
            now=now,
        )
        return ConversationRef.web(conversation_id)

    def list(self, namespace_id: str, agent_id: str | None = None) -> list[ScheduledPlan]:
        if agent_id is not None:
            require_identifier("agent_id", agent_id)
        return [self._describe(plan) for plan in self._store.list(namespace_id, agent_id)]

    def _build(self, namespace_id: str, agent_id: str, *, name: str, prompt: str,
               cron_expr: str = "", timezone_name: str = DEFAULT_TIMEZONE,
               schedule_kind: str = "cron", interval_seconds: int | None = None,
               run_at: str | datetime | None = None, starts_at: str | datetime | None = None,
               ends_at: str | datetime | None = None) -> ScheduledPlan:
        require_identifier("agent_id", agent_id)
        self._colleagues(namespace_id, agent_id)
        title = _bounded("name", name, _NAME_LIMIT)
        task = _bounded("prompt", prompt, _PROMPT_LIMIT)
        now = _utc(self._clock())
        zone = timezone_name.strip() or DEFAULT_TIMEZONE
        try:
            ZoneInfo(zone)
            start, end, run = (_input_time(value) for value in (starts_at, ends_at, run_at))
            if end and (end <= now or (start and end <= start)):
                raise ValueError("invalid effective period")
            if schedule_kind == "cron":
                validate_cron_expression(cron_expr, timezone=zone)
                cursor = occurrence_after(cron_expr, max(now, start - timedelta(microseconds=1)) if start else now, zone)
            elif schedule_kind == "interval":
                if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds < 1:
                    raise ValueError("interval_seconds must be a positive integer")
                cursor = start if start and start > now else now + timedelta(seconds=interval_seconds)
            elif schedule_kind == "once":
                if not run or run <= now or (start and run < start):
                    raise ValueError("one-time schedule must be in the future and effective period")
                cursor = run
            else:
                raise ValueError("unknown schedule_kind")
            if end and cursor > end:
                raise ValueError("no occurrence within effective period")
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            raise PersistentAgentError(str(exc), code="SCHEDULE_INVALID") from exc
        return ScheduledPlan(namespace_id=namespace_id, plan_id=self._ids(), agent_id=agent_id,
                             name=title, prompt=task, cron_expr=cron_expr.strip(), timezone_name=zone,
                             enabled=True, next_fire_at=cursor, created_at=now, schedule_kind=schedule_kind,
                             interval_seconds=interval_seconds if schedule_kind == "interval" else None,
                             run_at=run if schedule_kind == "once" else None, starts_at=start, ends_at=end)

    def create(self, namespace_id: str, agent_id: str, *, name: str, prompt: str, cron_expr: str = "",
               timezone_name: str = DEFAULT_TIMEZONE, schedule_kind: str = "cron",
               interval_seconds: int | None = None, run_at: str | datetime | None = None,
               starts_at: str | datetime | None = None, ends_at: str | datetime | None = None) -> ScheduledPlan:
        plan = self._build(namespace_id, agent_id, name=name, prompt=prompt, cron_expr=cron_expr,
                           timezone_name=timezone_name, schedule_kind=schedule_kind, interval_seconds=interval_seconds,
                           run_at=run_at, starts_at=starts_at, ends_at=ends_at)
        self._store.insert(plan)
        self._store.save(plan)
        return self._describe(plan)

    def update(self, namespace_id: str, plan_id: str, *, name: str, prompt: str, cron_expr: str = "",
               timezone_name: str = DEFAULT_TIMEZONE, schedule_kind: str = "cron",
               interval_seconds: int | None = None, run_at: str | datetime | None = None,
               starts_at: str | datetime | None = None, ends_at: str | datetime | None = None) -> ScheduledPlan:
        with self._claim(namespace_id, plan_id):
            old = self._store.get(namespace_id, plan_id)
            plan = self._build(namespace_id, old.agent_id, name=name, prompt=prompt, cron_expr=cron_expr,
                               timezone_name=timezone_name, schedule_kind=schedule_kind, interval_seconds=interval_seconds,
                               run_at=run_at, starts_at=starts_at, ends_at=ends_at)
            plan = replace(plan, plan_id=old.plan_id, created_at=old.created_at, enabled=old.enabled)
            self._store.save(plan)
            return self._describe(plan)

    def _next(self, plan: ScheduledPlan, cursor: datetime) -> datetime | None:
        if plan.schedule_kind == "once":
            return None
        value = (cursor + timedelta(seconds=plan.interval_seconds) if plan.schedule_kind == "interval"
                 else occurrence_after(plan.cron_expr, cursor, plan.timezone_name))
        return None if plan.ends_at and value > plan.ends_at else value

    def set_enabled(self, namespace_id: str, plan_id: str, *, enabled: bool) -> ScheduledPlan:
        with self._claim(namespace_id, plan_id):
            plan = self._store.get(namespace_id, plan_id)
            now = _utc(self._clock())
            cursor = plan.next_fire_at
            if enabled:
                if (plan.ends_at and plan.ends_at <= now) or (plan.schedule_kind == "once" and (not cursor or cursor <= now)):
                    raise PersistentAgentError("schedule expired or completed; edit its execution time", code="SCHEDULE_INVALID")
                if cursor is None or cursor <= now:
                    cursor = self._next(plan, now)
                if cursor is None:
                    raise PersistentAgentError("schedule expired", code="SCHEDULE_INVALID")
            self._store.save_next(namespace_id, plan_id, cursor, enabled=enabled)
            return self._describe(self._store.get(namespace_id, plan_id))

    def delete(self, namespace_id: str, plan_id: str) -> None:
        with self._claim(namespace_id, plan_id):
            self._store.delete(namespace_id, plan_id)

    def delete_agent(self, namespace_id: str, agent_id: str) -> None:
        with self._guard:
            plans = self._store.list(namespace_id, agent_id)
            if any((namespace_id, plan.plan_id) in self._busy for plan in plans):
                raise PersistentAgentError("schedule is being triggered; retry", code="CONFLICT")
            self._store.delete_agent(namespace_id, agent_id)

    def _runs(self, plan: ScheduledPlan) -> list[dict[str, object]]:
        rows = self._store.fires(plan.namespace_id, plan.plan_id)
        runs = []
        for row in rows:
            state = self._sessions.get_request(plan.namespace_id, row["request_id"])
            status = {"queued": "queued", "dispatched": "running"}.get(state.status)
            if status is None:
                status = state.outcome if state.outcome in {"completed", "failed"} else "stopped"
            runs.append({"request_id": row["request_id"], "scheduled_for": row["scheduled_for"],
                         "status": status, "description": state.outcome or ""})
        return sorted(runs, key=lambda run: self._sessions.get_request(plan.namespace_id, run["request_id"]).seq, reverse=True)

    def history(self, namespace_id: str, plan_id: str) -> dict[str, object]:
        plan = self._store.get(namespace_id, plan_id)
        return {"plan": self._describe(plan).to_dict(), "runs": self._runs(plan)}

    def _colleague_enabled(self, namespace_id: str, agent_id: str) -> bool:
        colleague = self._colleagues(namespace_id, agent_id)
        return getattr(colleague, "enabled", True) is not False

    async def run(self, namespace_id: str, plan_id: str) -> str:
        with self._claim(namespace_id, plan_id):
            plan = self._store.get(namespace_id, plan_id)
            if not self._colleague_enabled(plan.namespace_id, plan.agent_id):
                raise PersistentAgentError("disabled colleague does not run schedules", code="COLLEAGUE_DISABLED")
            return await self._accept(plan, _utc(self._clock()), manual=True)

    async def sweep(self, *, limit: int = CATCH_UP_LIMIT) -> int:
        if limit < 1:
            raise PersistentAgentError("schedule catch-up limit must be positive", code="SCHEDULE_INVALID")
        created = 0
        now = _utc(self._clock())
        for candidate in self._store.due(now):
            try:
                with self._claim(candidate.namespace_id, candidate.plan_id):
                    plan = self._store.get(candidate.namespace_id, candidate.plan_id)
                    if not plan.enabled:
                        continue
                    if not self._colleague_enabled(plan.namespace_id, plan.agent_id):
                        cursor = plan.next_fire_at
                        made = 0
                        while cursor is not None and cursor <= now and made < limit:
                            cursor = self._next(plan, cursor)
                            made += 1
                        self._store.save_next(
                            plan.namespace_id, plan.plan_id, cursor, enabled=cursor is not None
                        )
                        continue
                    cursor = plan.next_fire_at
                    made = 0
                    while cursor is not None and cursor <= now and made < limit:
                        if self._store.fired(plan.namespace_id, plan.plan_id, cursor) is None:
                            await self._accept(plan, cursor)
                            created += 1
                        cursor = self._next(plan, cursor)
                        made += 1
                        self._store.save_next(plan.namespace_id, plan.plan_id, cursor, enabled=cursor is not None)
            except PersistentAgentError:
                continue
        return created

    async def _accept(self, plan: ScheduledPlan, scheduled_for: datetime, *, manual: bool = False) -> str:
        snapshot = self._snapshots(plan.namespace_id, plan.agent_id)
        conversation = self._open_chat(plan, _utc(self._clock()))
        source_ref = source_of(plan.plan_id, scheduled_for)
        if manual:
            source_ref += f":manual:{uuid.uuid4().hex}"
        request_input = core.RequestInput(kind="scheduled", text=plan.prompt, reply_target=conversation.key)
        session = await asyncio.to_thread(
            self._sessions.resolve_session,
            plan.namespace_id,
            plan.agent_id,
            conversation,
            now=scheduled_for,
        )
        state, _created = await self._scheduler.accept(
            session,
            snapshot=snapshot,
            source_ref=source_ref,
            access=SCHEDULE_ACCESS,
            request_input=request_input,
            received_at=scheduled_for,
            route_snapshot=RequestRouteSnapshot(
                channel="web",
                target_id=conversation.key,
                source_message_id=source_ref,
            ),
        )
        self._store.record_fire(plan.namespace_id, plan.plan_id, scheduled_for, state.record.request_id, manual=manual)
        return state.record.request_id


def _bounded(name: str, value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersistentAgentError(f"missing {name}", code="SCHEDULE_INVALID")
    text = value.strip()
    if len(text) > limit:
        raise PersistentAgentError(f"{name} is too long", code="SCHEDULE_INVALID")
    return text


DEFAULT_SCHEDULE_INTERVAL = timedelta(seconds=30)


def _input_time(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return _utc(parsed)
