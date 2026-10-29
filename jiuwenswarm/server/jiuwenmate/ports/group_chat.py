"""Session-bound current-group reads; every read verifies current host membership."""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from jiuwenswarm.server.jiuwenmate.conversation_archive import ConversationArchive
from jiuwenswarm.server.jiuwenmate.conversation_resources import ConversationResources
from jiuwenswarm.server.jiuwenmate.conversation_search import ConversationSearch
from jiuwenswarm.server.jiuwenmate.group_chat_repository import GroupAccess, GroupChat, GroupChatRepository
from jiuwenswarm.server.jiuwenmate.group_search_cursor_repository import GroupSearchCursorRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.conversation_archive import time_micros
from jiuwenswarm.server.jiuwenmate.search_contracts import GroupMessageSearchResult
from openjiuwen.harness.persistent_agent import (
    ExecutionContext,
    GroupMessageQuery,
    GroupMessagesResult,
)

MembershipVerifier = Callable[[GroupAccess, GroupChat], Awaitable[str]]


async def unavailable_membership(access: GroupAccess, group: GroupChat) -> str:
    return "unknown"


class _SearchUnavailable(Exception):
    """A safe, non-disclosing reason to refuse a search or its continuation."""


class HostGroupChatPort:
    def __init__(self, groups: GroupChatRepository, archive: ConversationArchive, sessions,
                 *, namespace_id: str, agent_id: str, verify_membership: MembershipVerifier = unavailable_membership,
                 refresh_messages=None, search: ConversationSearch | None = None,
                 cursors: GroupSearchCursorRepository | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._groups, self._archive, self._sessions = groups, archive, sessions
        self.scope = (namespace_id, agent_id)
        self._verify = verify_membership
        self._refresh = refresh_messages
        self._search = search
        self._cursors = cursors
        self._clock = clock or (lambda: datetime.now(timezone.utc))

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
        if self._search is None or self._cursors is None:
            return self._search_result(execution, "unavailable", reason="Group history search is unavailable.")
        try:
            query = GroupMessageQuery.model_validate(query)
        except (ValidationError, TypeError, ValueError):
            return self._search_result(execution, "failed", reason="The group history search request is invalid.")
        try:
            self._identity(execution)
            session = await asyncio.to_thread(self._sessions.get_session, execution.namespace_id,
                                              execution.session_id)
            if (session.agent_id != execution.agent_id or
                    session.conversation.kind not in ("web_direct", "im_direct", "group")):
                return self._search_result(execution, "forbidden", reason="This execution cannot search group history.")
        except Exception:
            return self._search_result(execution, "forbidden", reason="This execution cannot search group history.")

        try:
            return await self._search_authorized(execution, query)
        except _SearchUnavailable as exc:
            return self._search_result(execution, "unavailable", reason=str(exc))
        except Exception:
            return self._search_result(execution, "failed", reason="Group history search failed.")

    async def sharing_sources(self, execution: ExecutionContext, query: GroupMessageQuery) -> tuple[dict, ...]:
        """Resolve the authorized search scope for the host's owner-approval gate only."""
        self._identity(execution)
        authorized = await self._authorized_groups(execution, query)
        targets, _ = self._resolve_targets(query, authorized)
        return tuple({"group_id": group_id, "name": authorized[group_id]["name"] or group_id,
                      "app_id": authorized[group_id]["access"].app_id,
                      "visible_from": authorized[group_id]["access"].visible_from.isoformat()}
                     for group_id in targets)

    async def _search_authorized(self, execution: ExecutionContext,
                                query: GroupMessageQuery) -> GroupMessageSearchResult:
        now = self._clock()
        query_digest = _query_digest(query)
        authorized = await self._authorized_groups(execution, query)
        target_ids, scope_all = self._resolve_targets(query, authorized)

        if not target_ids and query.cursor is None:
            # An empty, unrestricted authorization set is a confirmed empty scope.
            latest = await self._authorized_groups(execution, query)
            if latest:
                raise _SearchUnavailable("Group authorization changed; run the search again.")
            return self._search_result(execution, "empty", query_complete=True)

        cursor_state = None
        after_key = None
        target_states: dict[str, dict[str, object]] = {}
        incomplete_reason = None
        changed_during_read = False
        if query.cursor is not None:
            cursor_state = self._cursors.load(query.cursor, execution.namespace_id, execution.agent_id,
                                              query_digest, now=now)
            if cursor_state is None or not _valid_cursor_state(cursor_state):
                raise _SearchUnavailable("This search page is unavailable or expired; run the search again.")
            saved_ids = cursor_state["target_ids"]
            if (cursor_state["scope_all"] is not scope_all or saved_ids != list(target_ids)):
                raise _SearchUnavailable("Group authorization changed; run the search again.")
            saved_targets = {item["group_id"]: item for item in cursor_state["targets"]}
            if set(saved_targets) != set(target_ids):
                raise _SearchUnavailable("This search page is unavailable; run the search again.")
            for group_id in target_ids:
                current = authorized[group_id]
                saved = saved_targets[group_id]
                if not _same_authorization(saved, current):
                    raise _SearchUnavailable("Group authorization changed; run the search again.")
                target_states[group_id] = dict(saved)
            after = cursor_state["after"]
            after_key = (after[0], after[1], after[2])
        else:
            for group_id in target_ids:
                entry = authorized[group_id]
                try:
                    snapshot = await asyncio.to_thread(self._search.snapshot_group, execution.namespace_id, group_id)
                except PersistentAgentError as exc:
                    if exc.code == "NOT_FOUND":
                        incomplete_reason = "One or more authorized group archives are unavailable."
                    else:
                        incomplete_reason = "One or more authorized group archives could not be read."
                    continue
                target_states[group_id] = _target_state(entry, snapshot)
                if snapshot.has_gap:
                    incomplete_reason = "One or more authorized group archives have incomplete coverage."

        if cursor_state is not None:
            for group_id, saved in target_states.items():
                try:
                    snapshot = await asyncio.to_thread(self._search.snapshot_group, execution.namespace_id, group_id)
                except PersistentAgentError:
                    incomplete_reason = "One or more authorized group archives could not be read."
                    continue
                if (snapshot.content_revision != saved["content_revision"] or
                        snapshot.has_gap or saved["has_gap"]):
                    changed_during_read = True
                    incomplete_reason = "Group history changed; run a new search."

        pages = []
        if not changed_during_read:
            for group_id in target_ids:
                state = target_states.get(group_id)
                if state is None:
                    continue
                if state["has_gap"]:
                    incomplete_reason = "One or more authorized group archives have incomplete coverage."
                entry = authorized[group_id]
                try:
                    page = await asyncio.to_thread(
                        self._search.search_group_page,
                        execution.namespace_id,
                        group_id,
                        query,
                        visible_from=entry["access"].visible_from,
                        upper_ingest_seq=state["upper_ingest_seq"],
                        after_key=after_key,
                        limit=query.limit,
                    )
                except PersistentAgentError as exc:
                    incomplete_reason = (
                        "One or more authorized group archives are unavailable."
                        if exc.code == "NOT_FOUND" else "One or more authorized group archives could not be read."
                    )
                    continue
                pages.append((group_id, page))

        latest = await self._authorized_groups(execution, query)
        latest_ids, latest_scope_all = self._resolve_targets(query, latest)
        if latest_scope_all != scope_all or latest_ids != target_ids:
            raise _SearchUnavailable("Group authorization changed; run the search again.")
        for group_id in target_ids:
            before = target_states.get(group_id)
            current = latest[group_id]
            if before is not None and not _same_authorization(before, current):
                raise _SearchUnavailable("Group authorization changed; run the search again.")

        if not changed_during_read:
            for group_id in target_ids:
                if group_id not in target_states:
                    continue
                try:
                    snapshot = await asyncio.to_thread(self._search.snapshot_group, execution.namespace_id, group_id)
                except PersistentAgentError:
                    incomplete_reason = "One or more authorized group archives could not be read."
                    continue
                saved = target_states[group_id]
                if (snapshot.content_revision != saved["content_revision"] or
                        snapshot.has_gap != saved["has_gap"]):
                    changed_during_read = True
                    incomplete_reason = "Group history changed; run a new search."

        hits = tuple(sorted(
            (hit for _, page in pages for hit in page.hits),
            key=lambda hit: (-time_micros(hit.message.received_at), hit.group_id, hit.message.message_id),
        ))
        if changed_during_read:
            hits = ()
        visible_hits = hits[:query.limit]
        has_more = (len(hits) > query.limit or any(page.has_more for _, page in pages))
        all_targets_read = len(target_states) == len(target_ids) and len(pages) == len(target_ids)
        if not all_targets_read and incomplete_reason is None:
            incomplete_reason = "One or more authorized group archives could not be read."

        if incomplete_reason is not None:
            if not pages and not changed_during_read:
                return self._search_result(execution, "failed", reason="No authorized group archive could be read.")
            return self._search_result(execution, "incomplete", hits=visible_hits, reason=incomplete_reason)
        if has_more:
            last = visible_hits[-1]
            next_state = {
                "version": 1,
                "scope_all": scope_all,
                "target_ids": list(target_ids),
                "targets": [target_states[group_id] for group_id in target_ids],
                "after": [time_micros(last.message.received_at), last.group_id, last.message.message_id],
            }
            try:
                next_cursor = self._cursors.issue(
                    execution.namespace_id,
                    execution.agent_id,
                    query_digest,
                    next_state,
                    now=now,
                    expires_at=now + timedelta(minutes=15),
                )
            except PersistentAgentError:
                return self._search_result(execution, "incomplete", hits=visible_hits,
                                           reason="The next search page could not be saved; run the search again.")
            return self._search_result(
                execution, "incomplete", hits=visible_hits, cursor=next_cursor,
                reason="More matching group messages are available on the next page.",
            )
        if visible_hits:
            return self._search_result(execution, "found", hits=visible_hits, query_complete=True)
        return self._search_result(execution, "empty", query_complete=True)

    async def _authorized_groups(self, execution: ExecutionContext,
                                 query: GroupMessageQuery) -> dict[str, dict[str, object]]:
        try:
            unresolved = await asyncio.to_thread(self._groups.unresolved_accesses, execution.namespace_id,
                                                 execution.agent_id)
            unresolved_ids = {access.group_id for access in unresolved}
            if ((not query.group_ids and not query.group_names and unresolved_ids) or
                    unresolved_ids.intersection(query.group_ids)):
                raise _SearchUnavailable("Current group membership could not be verified.")
            accesses = await asyncio.to_thread(self._groups.active_accesses, execution.namespace_id,
                                               execution.agent_id)
        except _SearchUnavailable:
            raise
        except Exception as exc:
            raise _SearchUnavailable("Current group authorization is unavailable.") from exc
        by_group: dict[str, list[GroupAccess]] = {}
        for access in accesses:
            by_group.setdefault(access.group_id, []).append(access)
        if any(len(items) != 1 for items in by_group.values()):
            raise _SearchUnavailable("Current group authorization is ambiguous.")

        authorized: dict[str, dict[str, object]] = {}
        for group_id in sorted(by_group):
            access = by_group[group_id][0]
            try:
                group = await asyncio.to_thread(self._groups.get, execution.namespace_id, group_id)
                state = await self._verify(access, group)
            except Exception as exc:
                raise _SearchUnavailable("Current group membership could not be verified.") from exc
            if state == "left":
                continue
            if state != "active":
                raise _SearchUnavailable("Current group membership could not be verified.")
            name = await asyncio.to_thread(self._groups.known_name_of, execution.namespace_id, group_id)
            authorized[group_id] = {"access": access, "group": group, "name": name}
        return authorized

    @staticmethod
    def _resolve_targets(query: GroupMessageQuery,
                         authorized: dict[str, dict[str, object]]) -> tuple[tuple[str, ...], bool]:
        requested_ids = set(query.group_ids)
        requested_names = set(query.group_names)
        scope_all = not requested_ids and not requested_names
        if scope_all:
            return tuple(sorted(authorized)), True
        if requested_ids and not requested_ids.issubset(authorized):
            raise _SearchUnavailable("One or more requested group targets are unavailable.")
        if not requested_names:
            return tuple(sorted(requested_ids)), False

        matching_names: set[str] = set()
        for requested_name in sorted(requested_names):
            matching = sorted(
                group_id for group_id, item in authorized.items() if item["name"] == requested_name
            )
            if not matching:
                raise _SearchUnavailable("One or more requested group targets are unavailable.")
            if len(matching) > 1:
                if requested_ids:
                    matching = [group_id for group_id in matching if group_id in requested_ids]
                if len(matching) != 1:
                    if requested_ids and not matching:
                        raise _SearchUnavailable("One or more requested group targets are unavailable.")
                    ids = ", ".join(matching if requested_ids else sorted(
                        group_id for group_id, item in authorized.items() if item["name"] == requested_name
                    ))
                    raise _SearchUnavailable(f"This authorized group name is ambiguous; specify a group ID: {ids}"[:4096])
            matching_names.update(matching)
        targets = requested_ids & matching_names if requested_ids else matching_names
        if not targets:
            raise _SearchUnavailable("One or more requested group targets are unavailable.")
        return tuple(sorted(targets)), False

    @staticmethod
    def _search_result(execution: ExecutionContext, status: str, *, hits=(), cursor=None,
                       query_complete: bool = False, reason: str | None = None) -> GroupMessageSearchResult:
        return GroupMessageSearchResult(
            namespace_id=execution.namespace_id,
            agent_id=execution.agent_id,
            session_id=execution.session_id,
            request_id=execution.request_id,
            execution_id=execution.execution_id,
            source_ref=execution.source_ref,
            status=status,
            hits=hits,
            next_cursor=cursor,
            query_complete=query_complete,
            reason=reason,
        )


def _query_digest(query: GroupMessageQuery) -> str:
    payload = query.model_dump(mode="json", exclude={"cursor"})
    payload["group_ids"] = sorted(set(payload["group_ids"]))
    payload["group_names"] = sorted(set(payload["group_names"]))
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _target_state(entry: dict[str, object], snapshot) -> dict[str, object]:
    access = entry["access"]
    return {
        "group_id": access.group_id,
        "app_id": access.app_id,
        "source_ref": access.source_ref,
        "revision": access.revision,
        "visible_from": access.visible_from.isoformat(),
        "group_name": entry["name"],
        "upper_ingest_seq": snapshot.upper_ingest_seq,
        "content_revision": snapshot.content_revision,
        "has_gap": snapshot.has_gap,
    }


def _same_authorization(saved: dict[str, object], current: dict[str, object]) -> bool:
    access = current["access"]
    return (
        saved.get("group_id") == access.group_id
        and saved.get("app_id") == access.app_id
        and saved.get("source_ref") == access.source_ref
        and saved.get("revision") == access.revision
        and saved.get("visible_from") == access.visible_from.isoformat()
        and saved.get("group_name") == current["name"]
    )


def _valid_cursor_state(state: dict[str, object]) -> bool:
    if type(state.get("version")) is not int or state.get("version") != 1 or type(state.get("scope_all")) is not bool:
        return False
    target_ids = state.get("target_ids")
    targets = state.get("targets")
    after = state.get("after")
    if (not isinstance(target_ids, list) or not all(isinstance(item, str) for item in target_ids) or
            target_ids != sorted(set(target_ids))):
        return False
    if not isinstance(targets, list) or len(targets) != len(target_ids):
        return False
    if not isinstance(after, list) or len(after) != 3:
        return False
    if type(after[0]) is not int or not isinstance(after[1], str) or not isinstance(after[2], str):
        return False
    seen = set()
    for item in targets:
        if not isinstance(item, dict):
            return False
        group_id = item.get("group_id")
        if not isinstance(group_id, str) or group_id in seen:
            return False
        seen.add(group_id)
        if (not isinstance(item.get("app_id"), str) or not isinstance(item.get("source_ref"), str) or
                type(item.get("revision")) is not int or
                not isinstance(item.get("visible_from"), str) or
                (item.get("group_name") is not None and not isinstance(item.get("group_name"), str)) or
                type(item.get("upper_ingest_seq")) is not int or type(item.get("content_revision")) is not int or
                type(item.get("has_gap")) is not bool):
            return False
    return seen == set(target_ids)
