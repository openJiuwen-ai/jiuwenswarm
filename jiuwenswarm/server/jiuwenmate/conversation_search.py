"""Literal search in one authorized group; cross-group orchestration belongs to JM-21."""

from dataclasses import dataclass
from datetime import datetime

from jiuwenswarm.server.jiuwenmate.conversation_archive import (
    ConversationArchive,
    _retain_latest,
    message_reference,
    time_micros,
)
from jiuwenswarm.server.jiuwenmate.search_contracts import GroupMessageSearchHit
from openjiuwen.harness.persistent_agent import GroupMessage, GroupMessageQuery
from jiuwenswarm.server.jiuwenmate.group_chat_repository import utc_time
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError, require_identifier


@dataclass(frozen=True, slots=True)
class GroupSearchSnapshot:
    upper_ingest_seq: int
    content_revision: int
    has_gap: bool


@dataclass(frozen=True, slots=True)
class GroupSearchPage:
    hits: tuple[GroupMessageSearchHit, ...]
    has_more: bool


class ConversationSearch:
    def __init__(self, archive: ConversationArchive) -> None:
        self._archive = archive

    def snapshot_group(self, namespace_id: str, group_id: str) -> GroupSearchSnapshot:
        """Capture the archive watermark and content state without creating a missing archive."""
        with self._archive.connection(namespace_id, group_id) as conn:
            _retain_latest(conn)
            info = conn.execute("SELECT content_revision, gap FROM archive_info WHERE singleton=1").fetchone()
            if info is None:
                raise PersistentAgentError("group archive unavailable", code="ARCHIVE_UNAVAILABLE")
            upper = conn.execute("SELECT COALESCE(MAX(ingest_seq), 0) FROM messages").fetchone()[0]
            return GroupSearchSnapshot(upper_ingest_seq=upper, content_revision=info["content_revision"],
                                       has_gap=bool(info["gap"]))

    def search_group_page(
        self,
        namespace_id: str,
        group_id: str,
        query: GroupMessageQuery,
        *,
        visible_from: datetime,
        upper_ingest_seq: int,
        after_key: tuple[int, str, str] | None,
        limit: int,
    ) -> GroupSearchPage:
        """Read a stable, bounded page in the global received-time/group/message order."""
        query = GroupMessageQuery.model_validate(query)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise PersistentAgentError("invalid group search page limit")
        if not isinstance(upper_ingest_seq, int) or isinstance(upper_ingest_seq, bool) or upper_ingest_seq < 0:
            raise PersistentAgentError("invalid group search watermark")
        if after_key is not None:
            if (not isinstance(after_key, tuple) or len(after_key) != 3 or
                    not isinstance(after_key[0], int) or isinstance(after_key[0], bool)):
                raise PersistentAgentError("invalid group search position")
            require_identifier("group search position group", after_key[1])
            require_identifier("group search position message", after_key[2])

        visible = utc_time(visible_from)
        since = utc_time(query.since) if query.since is not None else None
        until = utc_time(query.until) if query.until is not None else None
        terms = tuple(term.casefold() for term in query.search_terms)
        group_name = self._archive.groups.name_of(namespace_id, group_id)
        matches: list[GroupMessageSearchHit] = []
        with self._archive.connection(namespace_id, group_id) as conn:
            rows = conn.execute(
                "SELECT payload FROM messages WHERE ingest_seq<=? AND received_at_us>=? "
                "ORDER BY received_at_us DESC, message_id ASC",
                (upper_ingest_seq, time_micros(visible)),
            )
            for row in rows:
                message = GroupMessage.model_validate_json(row["payload"])
                received = utc_time(message.received_at)
                if received < visible or (since is not None and received < since) or (until is not None and received > until):
                    continue
                key = (time_micros(received), group_id, message.message_id)
                if after_key is not None and not _key_is_after(key, after_key):
                    continue
                if query.sender_id is not None and message.sender_id != query.sender_id:
                    continue
                folded_text = message.text.casefold() if terms and message.text is not None else None
                if terms and (folded_text is None or not any(term in folded_text for term in terms)):
                    continue
                matches.append(
                    GroupMessageSearchHit(
                        group_id=group_id,
                        group_name=group_name,
                        message_ref=message_reference(group_id, message.message_id),
                        message=message,
                    )
                )
                if len(matches) > limit:
                    break
        return GroupSearchPage(hits=tuple(matches[:limit]), has_more=len(matches) > limit)

    def search_group(self, namespace_id: str, group_id: str, query: GroupMessageQuery,
                     *, visible_from: datetime) -> tuple[GroupMessageSearchHit, ...]:
        query = GroupMessageQuery.model_validate(query)
        terms = tuple(term.casefold() for term in query.search_terms)
        matches = []
        with self._archive.connection(namespace_id, group_id) as conn:
            for row in conn.execute("SELECT payload FROM messages"):
                message = GroupMessage.model_validate_json(row["payload"])
                if message.received_at < utc_time(visible_from):
                    continue
                if query.since is not None and message.received_at < query.since:
                    continue
                if query.until is not None and message.received_at > query.until:
                    continue
                if query.sender_id is not None and message.sender_id != query.sender_id:
                    continue
                folded_text = message.text.casefold() if terms and message.text is not None else None
                if terms and (folded_text is None or not any(term in folded_text for term in terms)):
                    continue
                matches.append(message)
        matches.sort(key=lambda item: (-item.received_at.timestamp(), item.message_id))
        name = self._archive.groups.name_of(namespace_id, group_id)
        return tuple(GroupMessageSearchHit(group_id=group_id, group_name=name, message_ref=message_reference(group_id, item.message_id),
                                          message=item) for item in matches[:query.limit])


def _key_is_after(key: tuple[int, str, str], after_key: tuple[int, str, str]) -> bool:
    """Compare keys for time descending, then group and message ID ascending."""
    return key[0] < after_key[0] or (key[0] == after_key[0] and key[1:] > after_key[1:])
