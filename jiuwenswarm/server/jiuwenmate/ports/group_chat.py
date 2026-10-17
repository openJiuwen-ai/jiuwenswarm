"""Session-bound current-group reads; every read verifies current host membership."""

import asyncio
from collections.abc import Awaitable, Callable

from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive
from jiuwenswarm.server.jiuwenmate.conversation_resources import ConversationResources
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupAccess, GroupChat, GroupChatRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from openjiuwen.harness.persistent_agent import ExecutionContext, GroupMessagesResult, GroupMessageSearchResult

MembershipVerifier = Callable[[GroupAccess, GroupChat], Awaitable[str]]


async def unavailable_membership(access: GroupAccess, group: GroupChat) -> str:
    return "unknown"


class HostGroupChatPort:
    def __init__(self, groups: GroupChatRepository, archive: ConversationArchive, sessions,
                 *, namespace_id: str, agent_id: str, verify_membership: MembershipVerifier = unavailable_membership,
                 refresh_messages=None) -> None:
        self._groups, self._archive, self._sessions = groups, archive, sessions
        self.scope = (namespace_id, agent_id)
        self._verify = verify_membership
        self._refresh = refresh_messages

    def _identity(self, execution: ExecutionContext) -> None:
        if self.scope != (execution.namespace_id, execution.agent_id):
            raise PersistentAgentError("group port belongs to another Agent", code="FORBIDDEN")

    async def _access(self, execution: ExecutionContext):
        self._identity(execution)
        session = await asyncio.to_thread(self._sessions.get_session, execution.namespace_id, execution.session_id)
        if session.agent_id != execution.agent_id or session.conversation.kind != "group":
            raise PersistentAgentError("current Session is not this Agent's group", code="FORBIDDEN")
        group_id = session.conversation.key
        group = await asyncio.to_thread(self._groups.get, execution.namespace_id, group_id)
        access = await asyncio.to_thread(self._groups.access_for, execution.namespace_id, execution.agent_id, group_id)
        if access.status != "active" or await self._verify(access, group) != "active":
            raise PersistentAgentError("current group membership unavailable", code="FORBIDDEN")
        return access, group

    async def recent_messages(self, execution: ExecutionContext, cursor: str | None = None,
                              limit: int = 20) -> GroupMessagesResult:
        access, group = await self._access(execution)
        result = await asyncio.to_thread(self._archive.recent, execution.namespace_id, group.group_chat_id,
                                         cursor=cursor, limit=limit, visible_from=access.visible_from,
                                         reader_id=execution.agent_id)
        messages = await self._checked_messages(execution, access, group, result.messages)
        if hasattr(self._sessions, "session_requests"):
            requests = await asyncio.to_thread(self._sessions.session_requests, execution.namespace_id, execution.session_id)
            queued = {state.record.request_input.trigger_message_id for state in requests if state.status == "queued"}
            messages = tuple(item.model_copy(update={"queued_request": item.message_id in queued}) for item in messages)
        return GroupMessagesResult(
            namespace_id=execution.namespace_id, agent_id=execution.agent_id, session_id=execution.session_id,
            request_id=execution.request_id, execution_id=execution.execution_id, source_ref=execution.source_ref,
            status=result.status, messages=messages, next_cursor=result.next_cursor,
            scanned_through=result.scanned_through, reason=result.reason,
        )

    async def current_trigger(self, execution: ExecutionContext, message_id: str):
        """Host-only preflight: queued trigger input must not bypass current message state."""
        access, group = await self._access(execution)
        messages = await asyncio.to_thread(self._archive.current_messages, execution.namespace_id,
                                          group.group_chat_id, (message_id,))
        if messages[0].received_at < access.visible_from:
            raise PersistentAgentError("trigger is outside readable history", code="FORBIDDEN")
        return (await self._checked_messages(execution, access, group, messages))[0]

    async def _checked_messages(self, execution, access, group, messages):
        if self._refresh is not None and messages:
            messages = await self._refresh(access, group, messages)
        checked, _ = await self._access(execution)
        if checked != access:
            raise PersistentAgentError("group authorization changed during read", code="FORBIDDEN")
        latest = await asyncio.to_thread(self._archive.current_messages, execution.namespace_id,
                                        group.group_chat_id, tuple(item.message_id for item in messages))
        unreadable = {item.message_id for item in messages if item.status == "unavailable"}
        messages = tuple(ConversationResources.readable_message(item.model_copy(update={"text": None,
                         "attachment_refs": (), "status": "unavailable"})
                         if item.message_id in unreadable and item.status != "recalled" else item) for item in latest)
        return messages

    async def search_messages(self, execution: ExecutionContext, query) -> GroupMessageSearchResult:
        self._identity(execution)
        return GroupMessageSearchResult(
            namespace_id=execution.namespace_id, agent_id=execution.agent_id, session_id=execution.session_id,
            request_id=execution.request_id, execution_id=execution.execution_id, source_ref=execution.source_ref,
            status="unavailable", query_complete=False, reason="Cross-group search is not opened yet (JM-21).",
        )
