"""Literal search in one authorized group; cross-group orchestration belongs to JM-21."""

from datetime import datetime

from openjiuwen.harness.persistent_agent import GroupMessage, GroupMessageQuery, GroupMessageSearchHit
from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive, message_reference
from jiuwenswarm.server.jiuwenmate.group_chat_repository import utc_time


class ConversationSearch:
    def __init__(self, archive: ConversationArchive) -> None:
        self._archive = archive

    def search_group(self, namespace_id: str, group_id: str, query: GroupMessageQuery,
                     *, visible_from: datetime) -> tuple[GroupMessageSearchHit, ...]:
        query = GroupMessageQuery.model_validate(query)
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
                if query.query is not None and (message.text is None or query.query.casefold() not in message.text.casefold()):
                    continue
                matches.append(message)
        matches.sort(key=lambda item: (-item.received_at.timestamp(), item.message_id))
        name = self._archive.groups.name_of(namespace_id, group_id)
        return tuple(GroupMessageSearchHit(group_id=group_id, group_name=name, message_ref=message_reference(group_id, item.message_id),
                                          message=item) for item in matches[:query.limit])
