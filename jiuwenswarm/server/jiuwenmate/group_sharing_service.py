"""Owner-authorized release of bounded cross-group search snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from openjiuwen.harness import persistent_agent as core

from jiuwenswarm.server.jiuwenmate.group_share_repository import GroupShareRepository
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.web_conversations import WebConversationStore

APPROVAL_CONVERSATION = "sharing-approvals"
def _digest(query: core.GroupMessageQuery, source_group_id: str | None = None) -> str:
    payload = query.model_dump(mode="json")
    if source_group_id is not None:
        payload["approval_source_group_id"] = source_group_id
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _safe_result(execution, reason: str, status: str = "forbidden"):
    return core.GroupMessageSearchResult(
        **core.InvocationRef.from_execution(execution).model_dump(),
        status=status, hits=(), next_cursor=None, query_complete=False, reason=reason,
    )


class GroupSharingService:
    """The host owns candidate data, owner identity and immutable delivery audiences."""

    def __init__(self, *, service, sessions, groups, archive, repository: GroupShareRepository, clock=None,
                 verify_membership=None):
        self.repository = repository
        self._service, self._sessions = service, sessions
        self._groups, self._archive = groups, archive
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._verify_membership = verify_membership

    def scope(self, execution, query) -> tuple[dict[str, Any], str | None]:
        state = self._sessions.get_request(execution.namespace_id, execution.request_id)
        record = state.record
        if (record.agent_id, record.session_id, record.access.principal_ref) != (
            execution.agent_id, execution.session_id, execution.principal_ref,
        ):
            raise PersistentAgentError("sharing request identity mismatch", code="FORBIDDEN")
        session = self._sessions.get_session(execution.namespace_id, execution.session_id)
        route = record.route_snapshot
        if route is None:
            raise PersistentAgentError("sharing destination unavailable", code="FORBIDDEN")
        resolver = getattr(self._service, "connection_of", None)
        connection = resolver(execution.namespace_id, execution.agent_id) if resolver is not None else None
        config_id = connection.config_id if connection is not None else execution.agent_id
        current_group = session.conversation.key if session.conversation.kind == "group" else None
        target_name = route.target_id
        if current_group is not None:
            target_name = self._groups.known_name_of(execution.namespace_id, current_group) or "群聊"
        source = record.request_input.source
        return {
            "namespace_id": execution.namespace_id, "agent_id": execution.agent_id,
            "config_id": config_id, "request_id": execution.request_id,
            "principal_ref": execution.principal_ref,
            "requester_name": getattr(source, "sender_name", "用户"),
            "target": {"channel": route.channel, "app_id": route.app_id or "", "id": route.target_id,
                       "kind": "group" if current_group else "direct", "name": target_name},
            "query": query.model_dump(mode="json"),
        }, current_group

    def notify_owner(self, scope: dict[str, Any]) -> None:
        """Create a durable owner-only conversation without making a model request."""
        WebConversationStore(self.repository.db_path).touch(
            scope["namespace_id"], scope["config_id"], APPROVAL_CONVERSATION,
            text="跨群分享审批", now=self._clock(),
        )

    async def enrich_owner_view(self, data: dict) -> None:
        """Resolve display labels in the destination without changing frozen consent or identities."""
        from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import feishu_member_names

        names_by_route = {}
        for approval in data["approvals"]:
            label = approval["requester_name"]
            try:
                state = await asyncio.to_thread(self._sessions.get_request, "local", approval["request_id"])
                record = state.record
                source = record.request_input.source
                sender_id = getattr(source, "sender_id", None)
                if label and label != sender_id and not label.startswith("ou_"):
                    continue
                approval["requester_name"] = ""
                route = record.route_snapshot
                if (route is None or route.channel != "feishu" or not route.target_id.startswith("oc_")
                        or not sender_id or not route.app_id):
                    continue
                key = (route.target_id, route.app_id)
                if key not in names_by_route:
                    names_by_route[key] = await asyncio.to_thread(feishu_member_names, route.target_id, (route.app_id,))
                name = names_by_route[key].get(sender_id, "")
                if isinstance(name, str) and name.strip() and name.strip() != sender_id:
                    approval["requester_name"] = name.strip()[:512]
            except Exception:
                # A failed profile lookup must not fail the inbox or expose a raw open_id.
                if label.startswith("ou_"):
                    approval["requester_name"] = ""

    async def authorize(self, execution, query, result, source_groups):
        if result.status not in {"found", "empty", "incomplete"}:
            return result
        try:
            scope, current_group = await asyncio.to_thread(self.scope, execution, query)
        except PersistentAgentError:
            return _safe_result(execution, "Cross-group sharing destination could not be verified.")

        batch_digest = _digest(query)
        source_by_id = {item["group_id"]: item for item in source_groups}
        frozen_batch = await asyncio.to_thread(
            self.repository.for_batch, execution.namespace_id, execution.agent_id,
            execution.request_id, batch_digest,
        )
        authorized_ids = {item["group_id"] for item in source_groups}
        if any(hit.group_id not in authorized_ids for hit in result.hits):
            return _safe_result(execution, "Source group authorization changed during search.", status="unavailable")

        current_hits = tuple(hit for hit in result.hits if hit.group_id == current_group)
        rows = frozen_batch
        if not rows:
            hits_by_source: dict[str, list] = {}
            for hit in result.hits:
                if hit.group_id != current_group:
                    hits_by_source.setdefault(hit.group_id, []).append(hit)
            if not hits_by_source:
                return result
            snapshots = []
            for source_group_id in sorted(hits_by_source):
                source = source_by_id.get(source_group_id)
                if source is None:
                    return _safe_result(
                        execution, "Source group authorization changed during search.", status="unavailable",
                    )
                source_scope = {
                    **scope,
                    "search_digest": batch_digest,
                    "source_authorizations": {
                        source_group_id: {
                            key: source[key] for key in ("app_id", "visible_from") if key in source
                        },
                    },
                }
                source_result = result.model_copy(update={"hits": tuple(hits_by_source[source_group_id])})
                source_digest = _digest(query, source_group_id)
                snapshots.append((source_scope, source_digest, source_result.model_dump(mode="json"), [source]))
            try:
                rows = await asyncio.to_thread(
                    self.repository.create_or_get_batch, snapshots, now=self._clock(),
                )
            except PersistentAgentError:
                return _safe_result(
                    execution, "The sharing preview is too large or unavailable; narrow the search.",
                    status="unavailable",
                )
            if not rows:
                return _safe_result(
                    execution, "Cross-group sharing decision is unavailable.", status="unavailable",
                )

        pending_rows = []
        approved_hits = []
        denied_source = False
        base_result = result
        for row in rows:
            if len(row["source_groups"]) != 1:
                return _safe_result(
                    execution, "Sharing approval must contain exactly one source group.", status="unavailable",
                )
            source_group_id = row["source_groups"][0]["group_id"]
            source = source_by_id.get(source_group_id)
            if source is None or source_group_id == current_group:
                return _safe_result(
                    execution, "Source group access changed; submit a new request.", status="unavailable",
                )
            expected_authorization = {
                source_group_id: {
                    key: source[key] for key in ("app_id", "visible_from") if key in source
                },
            }
            source_scope = row["scope"]
            if any(source_scope.get(key) != scope.get(key) for key in (
                "namespace_id", "config_id", "agent_id", "request_id", "principal_ref", "query",
            )):
                return _safe_result(execution, "Sharing approval belongs to a different request.")
            if any(source_scope["target"][key] != scope["target"][key]
                   for key in ("channel", "app_id", "kind", "id")):
                return _safe_result(execution, "Sharing approval belongs to a different audience.")
            if source_scope.get("search_digest") != batch_digest:
                return _safe_result(execution, "Sharing approval belongs to a different search.")
            if source_scope.get("source_authorizations") != expected_authorization:
                return _safe_result(execution, "Source group access changed; submit a new request.", status="unavailable")
            if row["status"] == "pending":
                await asyncio.to_thread(self.repository.authorize_from_grants, row["approval_id"], now=self._clock())
                row = await asyncio.to_thread(self.repository.get, row["approval_id"])
            if row["status"] == "pending":
                pending_rows.append(row)
                continue
            if row["status"] in {"rejected", "expired"}:
                denied_source = True
                continue
            if row["status"] not in {"approved_once", "approved_permanent"}:
                return _safe_result(execution, "Cross-group sharing decision is unavailable.", status="unavailable")
            if row["status"] == "approved_permanent" and not await asyncio.to_thread(
                self.repository.has_grant, source_scope, source_group_id,
            ):
                return _safe_result(execution, "Cross-group sharing authorization was revoked.")
            frozen = core.GroupMessageSearchResult.model_validate(row["result"])
            if any(hit.group_id != source_group_id for hit in frozen.hits):
                return _safe_result(execution, "Sharing approval contains an unexpected source group.",
                                    status="unavailable")
            if not await self._unchanged(execution.namespace_id, frozen):
                return _safe_result(
                    execution, "The approved source content changed or was recalled; submit a new request.",
                    status="unavailable",
                )
            approved_hits.extend(frozen.hits)
        if pending_rows:
            await asyncio.to_thread(self.notify_owner, pending_rows[0]["scope"])
            raise core.GroupMessageApprovalRequired(
                "跨群消息分享需要所有者逐个批准来源群。",
                approval_id=pending_rows[0]["approval_id"],
            )
        by_identity = {(hit.group_id, hit.message.message_id): hit for hit in (*approved_hits, *current_hits)}
        released_hits = tuple(sorted(
            by_identity.values(),
            key=lambda hit: (-hit.message.received_at.timestamp(), hit.group_id, hit.message.message_id),
        )[:query.limit])
        if denied_source:
            if not released_hits:
                return _safe_result(
                    execution, "部分来源群未获批准或审批已过期，未返回其消息。", status="unavailable",
                )
            return base_result.model_copy(update={
                "hits": released_hits,
                "status": "incomplete",
                "query_complete": False,
                "reason": "部分来源群未获批准或审批已过期，已省略这些消息。",
            })
        return base_result.model_copy(update={"hits": released_hits})

    async def _unchanged(self, namespace_id, result) -> bool:
        try:
            for hit in result.hits:
                messages = await asyncio.to_thread(
                    self._archive.current_messages, namespace_id, hit.group_id, (hit.message.message_id,),
                )
                if len(messages) != 1 or messages[0] != hit.message:
                    return False
            return True
        except Exception:
            return False

    async def can_deliver(self, job: dict[str, Any]) -> bool:
        """A revoked permanent grant also stops a result already waiting in the outbox."""
        namespace_id = str(job.get("namespace_id") or "local")
        rows = await asyncio.to_thread(
            self.repository.approved_for_request, namespace_id, str(job.get("request_id", "")),
        )
        record = None
        if rows:
            state = await asyncio.to_thread(self._sessions.get_request, namespace_id, str(job["request_id"]))
            record = state.record
            if record.route_snapshot is None:
                return False
        for row in rows:
            scope = row["scope"]
            target = scope["target"]
            # Public archive rows carry the immutable Request rather than all
            # send-job fields. Fill only absent fields from that trusted record.
            destination = {
                "channel": job.get("channel", record.route_snapshot.channel),
                "app_id": job.get("app_id", record.route_snapshot.app_id) or "",
                "id": job.get("target_id", record.route_snapshot.target_id),
                "kind": "group" if job.get("request_kind", record.request_input.kind) == "group_message" else "direct",
            }
            if job.get("agent_id", record.agent_id) != scope["agent_id"] or any(
                destination[key] != target[key] for key in ("channel", "app_id", "kind", "id")
            ):
                return False
            snapshot = await asyncio.to_thread(self.repository.get, row["approval_id"])
            if snapshot is None:
                return False
            frozen = core.GroupMessageSearchResult.model_validate(snapshot["result"])
            if not await self._unchanged(namespace_id, frozen):
                return False
            if self._verify_membership is not None:
                accesses = await asyncio.to_thread(
                    self._groups.active_accesses, namespace_id, row["scope"]["agent_id"],
                )
                for source in row["source_groups"]:
                    matching = [access for access in accesses if access.group_id == source["group_id"]]
                    if len(matching) != 1:
                        return False
                    group = await asyncio.to_thread(self._groups.get, namespace_id, source["group_id"])
                    if await self._verify_membership(matching[0], group) != "active":
                        return False
                    saved = row["scope"].get("source_authorizations", {}).get(source["group_id"], {})
                    if saved and saved != {"app_id": matching[0].app_id,
                                           "visible_from": matching[0].visible_from.isoformat()}:
                        return False
            if row["status"] == "approved_permanent":
                for source in row["source_groups"]:
                    if not await asyncio.to_thread(self.repository.has_grant, row["scope"], source["group_id"]):
                        return False
        return True


class GuardedGroupChatPort:
    """Current-group background is unchanged; explicit searches have a mandatory business gate."""

    def __init__(self, delegate, sharing: GroupSharingService):
        self._delegate, self._sharing = delegate, sharing

    @property
    def scope(self):
        return self._delegate.scope

    async def recent_messages(self, execution, cursor=None, limit=20):
        return await self._delegate.recent_messages(execution, cursor=cursor, limit=limit)

    async def current_trigger(self, execution, message_id):
        return await self._delegate.current_trigger(execution, message_id)

    async def search_messages(self, execution, query):
        try:
            before = await self._delegate.sharing_sources(execution, query)
        except Exception:
            return _safe_result(execution, "Current source group authorization is unavailable.", status="unavailable")
        result = await self._delegate.search_messages(execution, query)
        if result.status not in {"found", "empty", "incomplete"}:
            return result
        try:
            sources = await self._delegate.sharing_sources(execution, query)
        except Exception:
            return _safe_result(execution, "Current source group authorization is unavailable.", status="unavailable")
        def authorization(items):
            return {item["group_id"]: {key: item[key] for key in ("app_id", "visible_from") if key in item}
                    for item in items}
        if authorization(before) != authorization(sources):
            return _safe_result(execution, "Source group authorization changed during search.", status="unavailable")
        return await self._sharing.authorize(execution, query, result, sources)

