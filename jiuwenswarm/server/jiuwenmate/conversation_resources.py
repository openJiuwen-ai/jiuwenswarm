"""Authorized attachment references and idempotent archiving of confirmed outgoing messages."""

import json
from datetime import datetime

from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive
from jiuwenswarm.server.jiuwenmate.execution_models import RequestRecord
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from openjiuwen.harness.persistent_agent import GroupMessage


class ConversationResources:
    def __init__(self, archive: ConversationArchive, sessions: ExecutionRepository) -> None:
        self._archive, self._sessions = archive, sessions

    @staticmethod
    def readable_message(message: GroupMessage) -> GroupMessage:
        """Only references attributed to this exact platform message can leave the archive."""
        references = () if message.status in {"recalled", "unavailable"} else tuple(
            ref for ref in message.attachment_refs if ref.startswith(f"feishu:attachment:{message.message_id}:")
        )
        return message.model_copy(update={"attachment_refs": references})

    def archive_delivery(self, job: dict[str, object]) -> None:
        request = RequestRecord.from_dict(json.loads(job["record_json"]))
        session = self._sessions.get_session(request.namespace_id, request.session_id)
        if session.conversation.kind != "group":
            return
        route = request.route_snapshot
        if route is None or route.channel not in {"feishu", "dingtalk"} or not route.app_id:
            raise ValueError("confirmed group delivery has no platform route")
        group = self._archive.groups.get(request.namespace_id, session.conversation.key)
        if group.platform_chat_id != route.target_id:
            raise ValueError("delivery target differs from the Session group")
        payload = json.loads(job["payload_json"])
        message = GroupMessage(
            message_id=job["external_message_id"], sender_id=route.app_id, sender_name=request.agent_id,
            sender_kind="agent", received_at=datetime.fromisoformat(job["updated_at"]),
            text=payload["text"], reply_to=route.source_message_id,
        )
        self._archive.append(request.namespace_id, group.group_chat_id, message,
                             source_ref=f"delivery:{job['delivery_id']}")
