"""Read-only group records and execution traces for one colleague (JM-10).

The page lists groups this colleague can currently read, then the public
messages already stored for that group and the execution log of the colleague's
own Session in it. Another colleague's private window is never opened.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive
from jiuwenswarm.server.jiuwenmate.execution_models import ConversationRef
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

_TRACE_LIMIT = 200
_MESSAGE_LIMIT = 50
_SKIPPED_EVENTS = frozenset({"participation.decided", "window.switched"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _event_type(event) -> str:
    value = event.event_type
    return value.value if hasattr(value, "value") else str(value)


def _number(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usage_tokens(payload) -> tuple[int, int]:
    usage = getattr(payload, "usage", None)
    if usage is None:
        return 0, 0
    data = usage.model_dump() if hasattr(usage, "model_dump") else {}
    if not isinstance(data, dict):
        return 0, 0
    incoming = _number(data.get("input_tokens")) or _number(data.get("prompt_tokens"))
    outgoing = _number(data.get("output_tokens")) or _number(data.get("completion_tokens"))
    return incoming, outgoing


def _event_view(event) -> tuple[str, str, str, int, int] | None:
    """Map one saved event to role, lane and text. Internal window events stay out."""
    kind = _event_type(event)
    if kind in _SKIPPED_EVENTS:
        return None
    payload = event.payload
    if kind == "reply.delta":
        return "assistant", "model", str(getattr(payload, "text", "") or ""), 0, 0
    if kind == "progress.updated":
        return "system", "model", str(getattr(payload, "text", "") or ""), 0, 0
    if kind == "output.proposed":
        text = getattr(payload, "text", None) or getattr(payload, "file_path", None) or ""
        return "assistant", "model", str(text), 0, 0
    if kind == "model.completed":
        incoming, outgoing = _usage_tokens(payload)
        return "assistant", "model", str(getattr(payload, "model_call_id", "") or ""), incoming, outgoing
    if kind == "tool.intent":
        name = str(getattr(payload, "tool_name", "") or "")
        summary = str(getattr(payload, "args_summary", "") or "")
        return "tool", "tools", f"{name} {summary}".strip(), 0, 0
    if kind == "tool.completed":
        name = str(getattr(payload, "tool_name", "") or "")
        summary = str(getattr(payload, "result_summary", "") or getattr(payload, "status", "") or "")
        return "tool", "tools", f"{name} {summary}".strip(), 0, 0
    if kind == "execution.started":
        return "user", "input", str(getattr(payload, "goal", "") or ""), 0, 0
    if kind == "execution.paused":
        return "system", "input", str(getattr(payload, "reason", "") or ""), 0, 0
    if kind == "execution.final":
        return "system", "input", str(getattr(payload, "status", "") or ""), 0, 0
    return None


def trace_rows(requests: list[tuple[str, tuple]], *, origin: datetime | None = None) -> list[dict[str, object]]:
    """Fold request text and saved events into the trace rows the page already renders."""
    stamped: list[tuple[datetime, dict[str, object]]] = []
    for turn, (text, events) in enumerate(requests, start=1):
        if text.strip():
            moment = events[0].occurred_at if events else origin or _utc_now()
            stamped.append((moment, _row(f"request-{turn}", turn, "user", "input", text.strip(), 0, 0)))
        for event in events:
            viewed = _event_view(event)
            if viewed is None:
                continue
            role, lane, content, incoming, outgoing = viewed
            if not content.strip() and incoming == 0 and outgoing == 0:
                continue
            stamped.append((
                event.occurred_at,
                _row(event.event_id, turn, role, lane, content.strip(), incoming, outgoing),
            ))
            if len(stamped) >= _TRACE_LIMIT:
                break
        if len(stamped) >= _TRACE_LIMIT:
            break
    if not stamped:
        return []
    start = min(moment for moment, _row_data in stamped)
    rows = []
    previous = start
    for moment, row in stamped:
        row["start_ms"] = max(0, int((moment - start).total_seconds() * 1000))
        row["duration_ms"] = max(0, int((moment - previous).total_seconds() * 1000))
        previous = moment
        rows.append(row)
    return rows


def _row(row_id: str, turn: int, role: str, lane: str, content: str, incoming: int, outgoing: int) -> dict[str, object]:
    return {
        "id": row_id,
        "trace_id": row_id,
        "turn": turn,
        "role": role,
        "lane": lane,
        "content": content,
        "start_ms": 0,
        "duration_ms": 0,
        "input_tokens": incoming,
        "output_tokens": outgoing,
    }


class ChannelRecords:
    """Public group history and this colleague's execution trace for that group."""

    def __init__(
        self,
        groups: GroupChatRepository,
        archive: ConversationArchive,
        sessions: ExecutionRepository,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._groups = groups
        self._archive = archive
        self._sessions = sessions
        self._clock = clock

    def list_channels(self, namespace_id: str, agent_id: str) -> list[dict[str, object]]:
        """Groups with exactly one active binding. An ambiguous binding is omitted."""
        by_group: dict[str, list] = {}
        for access in self._groups.active_accesses(namespace_id, agent_id):
            by_group.setdefault(access.group_id, []).append(access)
        channels = []
        now = self._clock()
        for group_id, accesses in by_group.items():
            if len(accesses) != 1:
                continue
            group = self._groups.get(namespace_id, group_id)
            name = self._groups.known_name_of(namespace_id, group_id) or group_id
            channels.append({
                "id": group_id,
                "name": name,
                "platform": group.platform,
                "artifacts": 0,
                "token_usage": 0,
                "last_response_minutes": self._minutes_since_latest(namespace_id, group_id, accesses[0], now),
            })
        return channels

    def messages(self, namespace_id: str, agent_id: str, group_id: str) -> dict[str, object]:
        """Recent public messages visible to this colleague. A missing archive is an empty page."""
        access = self._readable(namespace_id, agent_id, group_id)
        try:
            page = self._archive.recent(
                namespace_id, group_id, cursor=None, limit=_MESSAGE_LIMIT, visible_from=access.visible_from,
                reader_id=agent_id,
            )
        except PersistentAgentError:
            return {"messages": [], "status": "empty"}
        return {
            "status": page.status,
            "messages": [
                {
                    "id": item.message_id,
                    "sender": item.sender_name,
                    "sender_kind": item.sender_kind,
                    "text": "" if item.text is None else item.text,
                    "status": item.status,
                    "received_at": item.received_at.isoformat(),
                    "attachments": len(item.attachment_refs),
                }
                for item in page.messages
            ],
        }

    def trace(self, namespace_id: str, agent_id: str, group_id: str) -> dict[str, object]:
        """Execution log of this colleague's Session in the group. No Session means an empty trace."""
        self._readable(namespace_id, agent_id, group_id)
        session = self._sessions.find_session(namespace_id, agent_id, ConversationRef.group(group_id))
        if session is None:
            return {"started_at": None, "rows": []}
        packed: list[tuple[str, tuple]] = []
        started_at = None
        for state in self._sessions.session_requests(namespace_id, session.session_id):
            if started_at is None:
                started_at = state.record.received_at.isoformat()
            events = []
            for execution in self._sessions.executions(namespace_id, state.record.request_id):
                events.extend(self._sessions.read_execution_log(
                    namespace_id, session.session_id, state.record.request_id, execution.execution_id,
                ))
            packed.append((str(getattr(state.record.request_input, "text", "") or ""), tuple(events)))
        return {"started_at": started_at, "rows": trace_rows(packed)}

    def _readable(self, namespace_id: str, agent_id: str, group_id: str):
        access = self._groups.access_for(namespace_id, agent_id, group_id)
        if access.status != "active":
            raise PersistentAgentError("group access is not active", code="FORBIDDEN")
        return access

    def _minutes_since_latest(self, namespace_id: str, group_id: str, access, now: datetime) -> int:
        try:
            page = self._archive.recent(
                namespace_id, group_id, cursor=None, limit=1, visible_from=access.visible_from, reader_id="",
            )
        except PersistentAgentError:
            return 0
        if not page.messages:
            return 0
        elapsed = now - page.messages[-1].received_at
        return max(0, int(elapsed.total_seconds() // 60))
