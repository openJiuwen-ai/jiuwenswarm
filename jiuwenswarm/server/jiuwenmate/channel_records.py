"""Read-only group records and execution traces for one colleague (JM-10).

The page lists groups this colleague can currently read, then the public
messages already stored for that group and the execution log of the colleague's
own Session in it. Another colleague's private window is never opened.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from urllib.parse import unquote

from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive
from jiuwenswarm.server.jiuwenmate.execution_models import ConversationRef
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupChatRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

_TRACE_LIMIT = 200
_MESSAGE_LIMIT = 100
_SKIPPED_EVENTS = frozenset({"participation.decided", "window.switched"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _event_type(event) -> str:
    value = event.event_type
    return value.value if hasattr(value, "value") else str(value)


def _number(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _attachment_views(group, message) -> list[dict[str, str]]:
    if message.status in {"recalled", "unavailable"}:
        return []
    attachments = []
    prefix = f"{group.platform}:outgoing-attachment:"
    for reference in message.attachment_refs:
        name = unquote(reference[len(prefix):]) if reference.startswith(prefix) else ""
        attachments.append({"name": name, "kind": "file"})
    return attachments


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


def _finish_reply(stamped: list[tuple[datetime, dict[str, object]]], open_reply: dict[str, object] | None) -> str | None:
    """Close one streamed reply. An empty fragment run is dropped."""
    if open_reply is None:
        return None
    open_reply["content"] = str(open_reply["content"]).strip()
    if open_reply["content"]:
        return str(open_reply["content"])
    stamped.pop()
    return None


def trace_rows(requests: list[tuple[str, tuple]], *, origin: datetime | None = None) -> list[dict[str, object]]:
    """Fold request text and saved events into the trace rows the page already renders.

    A model reply is stored as many ``reply.delta`` fragments, often one or two
    characters each. One model call becomes one assistant row. ``output.proposed``
    is omitted when its text is that same reply; a different candidate stays.
    """
    stamped: list[tuple[datetime, dict[str, object]]] = []
    stopped = False
    for turn, (text, events) in enumerate(requests, start=1):
        if text.strip():
            moment = events[0].occurred_at if events else origin or _utc_now()
            stamped.append((moment, _row(f"request-{turn}", turn, "user", "input", text.strip(), 0, 0)))
        open_reply: dict[str, object] | None = None
        last_reply: str | None = None
        for event in events:
            kind = _event_type(event)
            if kind == "reply.delta":
                fragment = str(getattr(event.payload, "text", "") or "")
                if not fragment:
                    continue
                if open_reply is None:
                    if len(stamped) >= _TRACE_LIMIT:
                        stopped = True
                        break
                    row = _row(event.event_id, turn, "assistant", "model", "", 0, 0)
                    row["content"] = fragment
                    stamped.append((event.occurred_at, row))
                    open_reply = row
                else:
                    open_reply["content"] = str(open_reply["content"]) + fragment
                continue
            closed = _finish_reply(stamped, open_reply)
            if closed is not None:
                last_reply = closed
            open_reply = None
            viewed = _event_view(event)
            if viewed is None:
                continue
            role, lane, content, incoming, outgoing = viewed
            file_path = str(getattr(event.payload, "file_path", "") or "")
            if kind == "output.proposed" and not file_path and content.strip() == (last_reply or ""):
                continue
            if not content.strip() and incoming == 0 and outgoing == 0:
                continue
            if len(stamped) >= _TRACE_LIMIT:
                stopped = True
                break
            stamped.append((
                event.occurred_at,
                _row(event.event_id, turn, role, lane, content.strip(), incoming, outgoing),
            ))
        _finish_reply(stamped, open_reply)
        if stopped or len(stamped) >= _TRACE_LIMIT:
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


def present_sender(
    message,
    *,
    agent_id: str,
    agent_names: Mapping[str, str],
    app_agents: Mapping[str, str],
    human_name: Callable[[str], str | None] | None = None,
    human_labels: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Name the real speaker. Only this colleague's own agent messages are marked own."""
    owner = app_agents.get(message.sender_id)
    if message.sender_name in agent_names and message.sender_kind == "agent":
        owner = owner or message.sender_name
    own = message.sender_kind == "agent" and (
        message.sender_id == agent_id or message.sender_name == agent_id or owner == agent_id
    )
    if own:
        label = agent_names.get(agent_id) or message.sender_name
    elif message.sender_kind == "agent":
        other_id = message.sender_id if message.sender_id in agent_names else owner
        label = agent_names.get(other_id or "", message.sender_name)
    else:
        stored = message.sender_name
        named = (human_labels or {}).get(message.sender_id)
        needs_lookup = (
            named is None
            and human_name is not None
            and (stored == message.sender_id or stored.startswith("ou_"))
        )
        looked_up = human_name(message.sender_id) if needs_lookup else None
        label = named or looked_up or stored
    return {
        "sender": label,
        "sender_id": message.sender_id,
        "sender_kind": message.sender_kind,
        "own": own,
    }


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
        agent_names: Mapping[str, str] | None = None,
        app_agents: Mapping[str, str] | None = None,
        human_name: Callable[[str], str | None] | None = None,
        member_names: Callable[[str, Sequence[str]], Mapping[str, str]] | None = None,
    ) -> None:
        self._groups = groups
        self._archive = archive
        self._sessions = sessions
        self._clock = clock
        self._agent_names = dict(agent_names or {})
        self._app_agents = dict(app_agents or {})
        self._human_name = human_name
        self._member_names = member_names

    def list_channels(self, namespace_id: str, agent_id: str) -> list[dict[str, object]]:
        """Groups with exactly one active binding. An ambiguous binding is omitted."""
        by_group: dict[str, list] = {}
        for access in self._groups.active_accesses(namespace_id, agent_id):
            by_group.setdefault(access.group_id, []).append(access)
        channels: list[tuple[dict[str, object], datetime | None]] = []
        now = self._clock()
        for group_id, accesses in by_group.items():
            if len(accesses) != 1:
                continue
            group = self._groups.get(namespace_id, group_id)
            name = self._groups.known_name_of(namespace_id, group_id) or group_id
            latest = self._latest_at(namespace_id, group_id, accesses[0])
            channel = {
                "id": group_id,
                "name": name,
                "platform": group.platform,
                "coverage": "received_events_only" if group.platform == "dingtalk" else "archive",
                "artifacts": 0,
                "token_usage": 0,
                "last_response_minutes": 0 if latest is None else max(0, int((now - latest).total_seconds() // 60)),
            }
            channels.append((channel, latest))
        channels.sort(key=lambda item: item[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return [item for item, _latest in channels]

    def messages(self, namespace_id: str, agent_id: str, group_id: str,
                 cursor: str | None = None) -> dict[str, object]:
        """A public history page visible to this colleague. A missing archive is an empty page."""
        access = self._readable(namespace_id, agent_id, group_id)
        try:
            page = self._archive.history_page(
                namespace_id, group_id, cursor=cursor, limit=_MESSAGE_LIMIT,
                visible_from=access.visible_from, reader_id=agent_id,
            )
        except PersistentAgentError:
            return {"messages": [], "status": "empty", "next_cursor": None}
        labels = self._human_labels(namespace_id, group_id)
        group = self._groups.get(namespace_id, group_id)
        return {
            "status": page.status,
            "next_cursor": page.next_cursor,
            "coverage": "received_events_only" if group.platform == "dingtalk" else "archive",
            "coverage_reason": page.reason,
            "messages": [
                {
                    "id": item.message_id,
                    **present_sender(
                        item,
                        agent_id=agent_id,
                        agent_names=self._agent_names,
                        app_agents=self._app_agents,
                        human_name=self._human_name,
                        human_labels=labels,
                    ),
                    "text": "" if item.text is None else item.text,
                    "status": item.status,
                    "received_at": item.received_at.isoformat(),
                    "attachments": _attachment_views(group, item),
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

    def _human_labels(self, namespace_id: str, group_id: str) -> Mapping[str, str]:
        """Feishu names visible inside this chat. A lookup failure keeps the stored label."""
        if self._member_names is None:
            return {}
        group = self._groups.get(namespace_id, group_id)
        if group.platform != "feishu":
            return {}
        app_ids = tuple(dict.fromkeys(
            item.app_id for item in self._groups.active_group_members(namespace_id, group_id)
        ))
        try:
            found = self._member_names(group.platform_chat_id, app_ids)
        except Exception:
            return {}
        return found if isinstance(found, Mapping) else {}

    def _readable(self, namespace_id: str, agent_id: str, group_id: str):
        access = self._groups.access_for(namespace_id, agent_id, group_id)
        if access.status != "active":
            raise PersistentAgentError("group access is not active", code="FORBIDDEN")
        return access

    def _latest_at(self, namespace_id: str, group_id: str, access) -> datetime | None:
        try:
            page = self._archive.recent(
                namespace_id, group_id, cursor=None, limit=1, visible_from=access.visible_from, reader_id="",
            )
        except PersistentAgentError:
            return None
        if not page.messages:
            return None
        return page.messages[-1].received_at
