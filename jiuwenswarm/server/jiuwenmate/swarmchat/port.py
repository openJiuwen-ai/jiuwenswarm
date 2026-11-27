"""Session-bound SwarmChat history; group membership is checked on every read."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from datetime import datetime, timedelta, timezone

from openjiuwen.agent_teams.schema.conversation import ConversationMessage
from openjiuwen.agent_teams.group_chat.handler import CONTEXT_TAIL
from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.harness.persistent_agent import (
    ExecutionContext,
    GroupMessage, GroupMessageQuery, GroupMessagesResult, InvocationRef,
)
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.search_contracts import (
    GroupArtifactSearchHit, GroupMessageSearchHit, GroupMessageSearchResult,
)
from jiuwenswarm.server.jiuwenmate.swarmchat.membership import groups_for, require_membership
from jiuwenswarm.server.runtime.group_service import GroupService


_FEISHU_ROSTER_COMMAND = re.compile(
    r"\blark-cli(?:\.cmd|\.exe)?[\"']?\s+(?:im\s+\+chat-members-list(?:\s|$)"
    r"|auth\s+login\b[^\r\n;&|]*\bim:chat\.members:read\b)",
    re.IGNORECASE,
)


def feishu_roster_cli_redirect(command: object) -> ToolOutput | None:
    if not isinstance(command, str) or not _FEISHU_ROSTER_COMMAND.search(command):
        return None
    message = (
        "飞书群成员请使用只读工具 list_group_members，指定 platform=feishu，并从群目录选择群。"
        "如果工具无法返回完整名单，请说明原因并结束本次查询，不要发起用户授权或生成二维码。"
    )
    return ToolOutput(success=False, data={"status": "use_roster_tool", "content": message}, error=message)


def converted(group: dict, message: ConversationMessage) -> GroupMessage:
    identities = {item["member_name"]: item["id"] for item in group["members"] if item["kind"] == "colleague"}
    active_names = {item["member_name"] for item in group["members"]}
    former = group.get("former_members") or {}
    sender_name = message.sender_name or message.sender
    if message.sender != "user" and message.sender not in active_names and message.sender in former:
        sender_name = f"{sender_name} (已离群)"
    return GroupMessage(
        message_id=message.message_id, sender_id=identities.get(message.sender, message.sender),
        sender_name=sender_name,
        sender_kind="human" if message.sender == "user" else "agent",
        received_at=datetime.fromtimestamp(message.timestamp / 1000, timezone.utc),
        text=message.content[:65536], mention_ids=tuple(identities.get(name, name) for name in message.mentions),
    )


class SwarmChatGroupChatPort:
    """Trusted host port over the existing group's message storage."""

    def __init__(self, sessions: Any, service: Any, cursors: Any) -> None:
        self._sessions = sessions
        self._service = service
        self._cursors = cursors
        self._groups = GroupService(None)
        self.scope = None
        from jiuwenswarm.server.jiuwenmate.swarmchat.consumption import SwarmChatFileImports

        self._imports = SwarmChatFileImports(sessions.db_path) if hasattr(sessions, "db_path") else None

    def bind(self, namespace_id: str, agent_id: str) -> SwarmChatGroupChatPort:
        self.scope = namespace_id, agent_id
        return self

    def validate_execution(self, execution: ExecutionContext) -> str:
        if self.scope != (execution.namespace_id, execution.agent_id) or execution.namespace_id != "local":
            raise PersistentAgentError("SwarmChat port identity mismatch", code="FORBIDDEN")
        colleague = self._service.get_colleague(execution.namespace_id, execution.agent_id)
        if colleague is None:
            raise PersistentAgentError("Colleague not found", code="NOT_FOUND")
        if not colleague.enabled:
            raise PersistentAgentError("Colleague is disabled", code="FORBIDDEN")
        connection = self._service.connection_of(execution.namespace_id, execution.agent_id)
        agent_id = connection.config_id if connection is not None else execution.agent_id
        session = self._sessions.get_session(execution.namespace_id, execution.session_id)
        if session.conversation.kind == "swarm_group":
            group, _ = require_membership(session.conversation.key, agent_id)
            if (group["user_id"] or "local") != execution.principal_ref:
                raise PersistentAgentError("SwarmChat owner changed", code="FORBIDDEN")
        return agent_id

    async def messages(self, group: dict) -> list[ConversationMessage]:
        return await self._groups.public_messages(
            group["group_id"], user_id=group["user_id"], channel_id=group["channel_id"],
        )

    async def prepare_tool_call(self, execution: ExecutionContext, tool_name: str, arguments: dict) -> Any:
        session = self._sessions.get_session(execution.namespace_id, execution.session_id)
        if session.conversation.kind == "swarm_group":
            self.validate_execution(execution)
            if tool_name == "bash":
                return feishu_roster_cli_redirect(arguments.get("command"))
        return None

    async def recent_messages(
        self, execution: ExecutionContext, cursor: str | None = None, limit: int = 20,
    ) -> GroupMessagesResult:
        agent_id = self.validate_execution(execution)
        session = self._sessions.get_session(execution.namespace_id, execution.session_id)
        group, _ = require_membership(session.conversation.key, agent_id)
        request = self._sessions.get_request(execution.namespace_id, execution.request_id).record
        trigger = request.request_input.trigger_message_id
        messages = await self.messages(group)
        end = next((i for i, item in enumerate(messages) if item.message_id == trigger), len(messages))
        start = 0
        if cursor:
            start = next((i + 1 for i, item in enumerate(messages) if item.message_id == cursor), 0)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("invalid group background limit")
        count = min(limit, CONTEXT_TAIL - 1)
        selected = messages[start:end][-count:]
        return GroupMessagesResult(
            **InvocationRef.from_execution(execution).model_dump(),
            status="found" if selected else "empty",
            messages=tuple(
                converted(group, item).model_copy(update={"text": item.content[:2000]}) for item in selected
            ),
            scanned_through=trigger or (messages[-1].message_id if messages else cursor),
        )

    async def search_content(self, execution: ExecutionContext, query: GroupMessageQuery) -> GroupMessageSearchResult:
        return await self.search_messages(execution, query)

    async def current_trigger(self, execution: ExecutionContext, message_id: str) -> GroupMessage | None:
        agent_id = self.validate_execution(execution)
        session = self._sessions.get_session(execution.namespace_id, execution.session_id)
        group, _ = require_membership(session.conversation.key, agent_id)
        return next((converted(group, item) for item in await self.messages(group)
                     if item.message_id == message_id), None)

    async def search_messages(self, execution: ExecutionContext, query: GroupMessageQuery) -> GroupMessageSearchResult:
        query = GroupMessageQuery.model_validate(query)
        reference = InvocationRef.from_execution(execution).model_dump()
        agent_id = self.validate_execution(execution)
        if query.platforms and "swarmchat" not in query.platforms:
            return GroupMessageSearchResult(**reference, status="empty", query_complete=True)
        session = self._sessions.get_session(execution.namespace_id, execution.session_id)
        owner = None
        if session.conversation.kind == "swarm_group":
            current, _ = require_membership(session.conversation.key, agent_id)
            owner = current["user_id"]
        candidates = groups_for(agent_id, owner=owner)
        requested = set(query.group_ids)
        names = set(query.group_names)
        selected = []
        for group in candidates:
            if requested and group["group_id"] not in requested:
                continue
            if names and group["name"] not in names:
                continue
            selected.append(group)
        if any(sum(group["name"] == name for group in selected) > 1 for name in names):
            return GroupMessageSearchResult(**reference, status="unavailable", query_complete=False,
                                            reason="The group name is ambiguous; specify a group ID.")
        if requested - {group["group_id"] for group in selected} or names - {group["name"] for group in selected}:
            return GroupMessageSearchResult(**reference, status="unavailable", query_complete=False,
                                            reason="The requested group is unavailable.")
        digest = hashlib.sha256(query.model_dump_json(exclude={"cursor"}).encode()).hexdigest()
        scope = sorted((group["group_id"], group.get("history_cleared_at")) for group in selected)
        now = datetime.now(timezone.utc)
        previous = None
        if query.cursor:
            saved = self._cursors.load(query.cursor, execution.namespace_id, execution.agent_id, digest, now=now)
            if saved is None or saved["scope"] != [list(item) for item in scope]:
                return GroupMessageSearchResult(**reference, status="unavailable", query_complete=False,
                                                reason="Search authorization changed or the cursor expired.")
            previous = tuple(saved["after"])
        hits = []
        words = [word.casefold() for word in (*query.keywords, *((query.query,) if query.query else ()))]
        if "messages" in query.content_types:
            for group in selected:
                for item in await self.messages(group):
                    message = converted(group, item)
                    if query.since and message.received_at < query.since:
                        continue
                    if query.until and message.received_at > query.until:
                        continue
                    if query.sender_id and query.sender_id != message.sender_id:
                        continue
                    if words and not any(word in (message.text or "").casefold() for word in words):
                        continue
                    key = (-message.received_at.timestamp(), group["group_id"], message.message_id)
                    if previous is not None and key <= previous:
                        continue
                    hits.append((key, GroupMessageSearchHit(
                        group_id=group["group_id"], group_name=group["name"], platform="swarmchat",
                        platform_chat_id=group["group_id"], message_ref=message.message_id, message=message,
                    )))
        if "files" in query.content_types:
            from jiuwenswarm.server.jiuwenmate.swarmchat.files import published_files, import_file

            for group in selected:
                for file in published_files(group):
                    if words and not any(word in file["name"].casefold() for word in words):
                        continue
                    created = datetime.fromtimestamp(file["created_at"], timezone.utc)
                    if query.since and created < query.since:
                        continue
                    if query.until and created > query.until:
                        continue
                    identity = hashlib.sha256(str(file["path"]).encode()).hexdigest()
                    key = (-created.timestamp(), group["group_id"], identity)
                    if previous is not None and key <= previous:
                        continue
                    hits.append((key, (group, file, identity, created)))
        hits.sort(key=lambda item: item[0])
        more = len(hits) > query.limit
        hits = hits[:query.limit]
        token = None
        if more:
            token = self._cursors.issue(execution.namespace_id, execution.agent_id, digest,
                                       {"scope": scope, "after": hits[-1][0]},
                                       now=now, expires_at=now + timedelta(minutes=15))
        # Recheck membership after awaits, before releasing any content.
        self.validate_execution(execution)
        for group in selected:
            current, _ = require_membership(group["group_id"], agent_id)
            if (current.get("history_cleared_at"), current["user_id"]) != (
                group.get("history_cleared_at"), group["user_id"],
            ):
                return GroupMessageSearchResult(**reference, status="unavailable", query_complete=False,
                                                reason="Group visibility changed while reading.")
        message_hits = []
        artifacts = []
        for _, hit in hits:
            if isinstance(hit, GroupMessageSearchHit):
                message_hits.append(hit)
                continue
            group, file, identity, created = hit
            source_session, name, file_digest, imported = import_file(
                self._service.workspaces, execution, agent_id, group, file,
            )
            self._imports.record(execution, imported, group["group_id"], str(file["path"]), file_digest)
            artifacts.append(GroupArtifactSearchHit(
                group_id=group["group_id"], group_name=group["name"], platform="swarmchat",
                platform_chat_id=group["group_id"], artifact_id=identity, artifact_kind="file",
                title=file["name"], file_name=name, file_size=file["size"], file_digest=f"sha256:{file_digest}",
                source_session_id=source_session, source_file_path=f"sessions/{source_session}/output/{name}",
                imported_path=imported, created_at=created,
            ))
        return GroupMessageSearchResult(
            **reference, status="incomplete" if more else "found" if hits else "empty",
            hits=tuple(message_hits), artifacts=tuple(artifacts), next_cursor=token, query_complete=not more,
            reason="More matching messages are available." if more else None,
        )


class CompositeGroupChatPort:
    """One search surface for IM and SwarmChat, preserving source authorization."""

    def __init__(self, im: Any, swarm: SwarmChatGroupChatPort, sessions: Any, cursors: Any) -> None:
        self._im = im
        self._swarm = swarm
        self._sessions = sessions
        self._cursors = cursors
        self.scope = swarm.scope

    def _background(self, execution: ExecutionContext) -> Any:
        session = self._sessions.get_session(execution.namespace_id, execution.session_id)
        return self._swarm if session.conversation.kind == "swarm_group" else self._im

    async def recent_messages(
        self, execution: ExecutionContext, cursor: str | None = None, limit: int = 20,
    ) -> GroupMessagesResult:
        return await self._background(execution).recent_messages(execution, cursor=cursor, limit=limit)

    async def current_trigger(self, execution: ExecutionContext, message_id: str) -> GroupMessage | None:
        return await self._background(execution).current_trigger(execution, message_id)

    async def sharing_sources(self, execution: ExecutionContext, query: GroupMessageQuery) -> tuple[dict, ...]:
        return await self._im.sharing_sources(execution, query)

    async def prepare_tool_call(self, execution: ExecutionContext, tool_name: str, arguments: dict) -> Any:
        redirect = await self._swarm.prepare_tool_call(execution, tool_name, arguments)
        if redirect is not None:
            return redirect
        prepare = getattr(self._im, "prepare_tool_call", None)
        return await prepare(execution, tool_name, arguments) if prepare is not None else None

    async def search_messages(self, execution: ExecutionContext, query: GroupMessageQuery) -> GroupMessageSearchResult:
        return await self.search_content(execution, query.model_copy(update={"content_types": ("messages",)}))

    async def _search_im(self, execution: ExecutionContext, query: GroupMessageQuery) -> GroupMessageSearchResult:
        search = getattr(self._im, "search_content", None)
        if search is None:
            # A bare IM port has message history only. Artifact search requires the sharing service.
            search = self._im.search_messages
        return await search(execution, query)

    async def search_content(self, execution: ExecutionContext, query: GroupMessageQuery) -> GroupMessageSearchResult:
        # Explicit platforms can use the original source's pagination unchanged.
        if query.platforms and "swarmchat" not in query.platforms:
            return await self._search_im(execution, query)
        agent_id = self._swarm.validate_execution(execution)
        swarm_groups = groups_for(agent_id)
        swarm_ids = {item["group_id"] for item in swarm_groups}
        swarm_names = {item["name"] for item in swarm_groups}
        explicit_swarm = query.platforms == ("swarmchat",)
        if explicit_swarm:
            return await self._swarm.search_messages(execution, query)
        only_swarm = bool(query.group_ids or query.group_names)
        only_swarm = only_swarm and set(query.group_ids) <= swarm_ids and set(query.group_names) <= swarm_names
        if only_swarm:
            return await self._swarm.search_messages(execution, query)
        ids_only_im = bool(query.group_ids) and swarm_ids.isdisjoint(query.group_ids)
        names_only_im = bool(query.group_names) and swarm_names.isdisjoint(query.group_names)
        if ids_only_im or names_only_im:
            return await self._search_im(execution, query)
        return await self._merged(execution, query)

    async def _merged(self, execution: ExecutionContext, query: GroupMessageQuery) -> GroupMessageSearchResult:
        reference = InvocationRef.from_execution(execution).model_dump()
        digest = hashlib.sha256(query.model_dump_json(exclude={"cursor"}).encode()).hexdigest()
        now = datetime.now(timezone.utc)
        state = None
        if query.cursor:
            state = self._cursors.load(query.cursor, execution.namespace_id, execution.agent_id, digest, now=now)
            if state is None or state.get("kind") != "composite":
                return GroupMessageSearchResult(**reference, status="unavailable", query_complete=False,
                                                reason="The combined search cursor expired.")
        until = datetime.fromisoformat(state["until"]) if state else query.until or now
        previous = tuple(state["after"]) if state else None
        hits = []
        incomplete = False
        agent_id = self._swarm.validate_execution(execution)
        swarm_groups = groups_for(agent_id)
        swarm_ids = {item["group_id"] for item in swarm_groups}
        swarm_names = {item["name"] for item in swarm_groups}
        for platform, search in (("swarmchat", self._swarm.search_content), ("im", self._search_im)):
            cursor = None
            is_swarmchat = platform == "swarmchat"
            source_ids = tuple(item for item in query.group_ids if (item in swarm_ids) == is_swarmchat)
            source_names = tuple(item for item in query.group_names if (item in swarm_names) == is_swarmchat)
            if query.group_ids and not source_ids:
                continue
            if query.group_names and not source_names:
                continue
            for _ in range(100):
                if platform == "swarmchat":
                    platforms = ("swarmchat",)
                else:
                    platforms = tuple(item for item in query.platforms if item != "swarmchat")
                    if not platforms:
                        platforms = ("feishu", "dingtalk", "welink")
                current = query.model_copy(update={"platforms": platforms, "cursor": cursor,
                                                   "until": until, "limit": 500,
                                                   "group_ids": source_ids, "group_names": source_names})
                result = await search(execution, current)
                if result.status in {"forbidden", "failed", "unavailable"}:
                    return result
                hits.extend(result.hits)
                hits.extend(result.artifacts)
                cursor = result.next_cursor
                if cursor is None:
                    incomplete |= result.status == "incomplete"
                    break
            else:
                incomplete = True
        snapshot = hashlib.sha256("".join(item.model_dump_json() for item in hits).encode()).hexdigest()
        if state is not None and state.get("snapshot") != snapshot:
            return GroupMessageSearchResult(**reference, status="unavailable", query_complete=False,
                                            reason="Search history or authorization changed; restart the search.")
        pairs = []
        for hit in hits:
            if isinstance(hit, GroupMessageSearchHit):
                key = (-hit.message.received_at.timestamp(), hit.group_id, hit.message.message_id)
            else:
                key = (-hit.created_at.timestamp(), hit.group_id, hit.artifact_id)
            if previous is None or key > previous:
                pairs.append((key, hit))
        pairs.sort(key=lambda item: item[0])
        more = len(pairs) > query.limit
        pairs = pairs[:query.limit]
        cursor = None
        if more:
            next_state = {
                "kind": "composite", "after": pairs[-1][0], "until": until.isoformat(), "snapshot": snapshot,
            }
            cursor = self._cursors.issue(
                execution.namespace_id, execution.agent_id, digest, next_state,
                now=now, expires_at=now + timedelta(minutes=15),
            )
        partial = more or incomplete
        partial_reason = "Additional or unavailable history exists; narrow the query if no cursor is provided."
        return GroupMessageSearchResult(
            **reference,
            status="incomplete" if partial else "found" if pairs else "empty",
            hits=tuple(item[1] for item in pairs if isinstance(item[1], GroupMessageSearchHit)),
            artifacts=tuple(item[1] for item in pairs if isinstance(item[1], GroupArtifactSearchHit)),
            next_cursor=cursor, query_complete=not partial,
            reason=partial_reason if partial else None,
        )
