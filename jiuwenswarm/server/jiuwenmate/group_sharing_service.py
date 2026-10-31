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


def _snapshot_content_types(result) -> list[str]:
    types = []
    if result.hits:
        types.append("messages")
    artifact_kinds = {item.artifact_kind for item in result.artifacts}
    if "file" in artifact_kinds:
        types.append("files")
    if "online_doc" in artifact_kinds:
        types.append("online_docs")
    return types


def _query_scope(query) -> dict:
    payload = core.GroupMessageQuery.model_validate(query).model_dump(mode="json")
    if not payload["platforms"]:
        payload.pop("platforms", None)
    return payload


def _digest(query: core.GroupMessageQuery, source_group_id: str | None = None) -> str:
    payload = _query_scope(query)
    if source_group_id is not None:
        payload["approval_source_group_id"] = source_group_id
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _safe_result(execution, reason: str, status: str = "forbidden"):
    return core.GroupMessageSearchResult(
        **core.InvocationRef.from_execution(execution).model_dump(),
        status=status, hits=(), artifacts=(), next_cursor=None, query_complete=False, reason=reason,
    )


class GroupSharingService:
    """The host owns candidate data, owner identity and immutable delivery audiences."""

    def __init__(self, *, service, sessions, groups, archive, repository: GroupShareRepository, files=None, clock=None,
                 verify_membership=None, cli=None):
        self.repository = repository
        self._service, self._sessions = service, sessions
        self._groups, self._archive = groups, archive
        self._files = files
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._verify_membership = verify_membership
        self._cli = cli

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

    async def enrich_owner_view(self, data: dict, namespace_id: str = "local") -> None:
        """Resolve display labels in the destination without changing frozen consent or identities."""
        from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import feishu_member_names

        # Older approvals/grants predate source-platform fields; the group mapping is immutable.
        for approval in data["approvals"]:
            source_by_id = {}
            for source in approval["source_groups"]:
                try:
                    group = await asyncio.to_thread(self._groups.get, namespace_id, source["group_id"])
                    source.update(platform=group.platform, platform_chat_id=group.platform_chat_id)
                except Exception:
                    pass
                source_by_id[source["group_id"]] = source
            for item in (*approval["messages"], *approval.get("artifacts", ())):
                source = source_by_id.get(item["group_id"], {})
                item.update(platform=source.get("platform"), platform_chat_id=source.get("platform_chat_id"))
        for grant in data.get("grants", ()):
            try:
                group = await asyncio.to_thread(self._groups.get, namespace_id, grant["source_group_id"])
                grant["source_platform"] = group.platform
            except Exception:
                pass
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
        candidates = (*result.hits, *result.artifacts)
        if any(hit.group_id not in authorized_ids for hit in candidates):
            return _safe_result(execution, "Source group authorization changed during search.", status="unavailable")

        current_hits = tuple(hit for hit in result.hits if hit.group_id == current_group)
        current_artifacts = tuple(hit for hit in result.artifacts if hit.group_id == current_group)
        rows = frozen_batch
        if not rows:
            denied = await asyncio.to_thread(self.repository.denied_groups, execution.namespace_id,
                                             execution.agent_id, execution.request_id)
            if any(hit.group_id in denied for hit in candidates):
                kept_hits = tuple(hit for hit in result.hits if hit.group_id not in denied)
                kept_artifacts = tuple(hit for hit in result.artifacts if hit.group_id not in denied)
                if not kept_hits and not kept_artifacts:
                    return _safe_result(execution, "所有者拒绝或审批已过期，本次请求不再申请此来源的跨群分享。")
                result = result.model_copy(update={"hits": kept_hits, "artifacts": kept_artifacts,
                    "status": "incomplete", "query_complete": False, "reason": "部分来源未获准分享。"})
            by_source: dict[str, dict[str, list]] = {}
            for hit in result.hits:
                if hit.group_id != current_group:
                    by_source.setdefault(hit.group_id, {"hits": [], "artifacts": []})["hits"].append(hit)
            for artifact in result.artifacts:
                if artifact.group_id != current_group:
                    by_source.setdefault(artifact.group_id, {"hits": [], "artifacts": []})["artifacts"].append(artifact)
            if not by_source:
                staged = await self._stage_approved_files(execution, result.artifacts)
                return result.model_copy(update={"artifacts": staged})
            snapshots = []
            for source_group_id in sorted(by_source):
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
                source_result = result.model_copy(update={
                    "hits": tuple(by_source[source_group_id]["hits"]),
                    "artifacts": tuple(by_source[source_group_id]["artifacts"]),
                })
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
        approved_artifacts = []
        denied_source = False
        base_result = result.model_copy(update=core.InvocationRef.from_execution(execution).model_dump())
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
                "namespace_id", "config_id", "agent_id", "request_id", "principal_ref",
            )) or _query_scope(source_scope.get("query")) != _query_scope(scope.get("query")):
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
            frozen = core.GroupMessageSearchResult.model_validate(row["result"])
            required_types = _snapshot_content_types(frozen)
            if row["status"] == "approved_permanent" and not await asyncio.to_thread(
                self.repository.has_grant, source_scope, source_group_id, required_types,
            ):
                return _safe_result(execution, "Cross-group sharing authorization was revoked.")
            if any(hit.group_id != source_group_id for hit in (*frozen.hits, *frozen.artifacts)):
                return _safe_result(execution, "Sharing approval contains an unexpected source group.",
                                    status="unavailable")
            if not await self._unchanged(execution.namespace_id, frozen):
                return _safe_result(
                    execution, "The approved source content changed or was recalled; submit a new request.",
                    status="unavailable",
                )
            approved_hits.extend(frozen.hits)
            approved_artifacts.extend(frozen.artifacts)
        if pending_rows:
            await asyncio.to_thread(self.notify_owner, pending_rows[0]["scope"])
            raise core.GroupContentApprovalRequired(
                "跨群内容分享需要所有者逐个批准来源群。",
                approval_id=pending_rows[0]["approval_id"],
            )
        by_identity = {(hit.group_id, hit.message.message_id): hit for hit in (*approved_hits, *current_hits)}
        released_hits = tuple(sorted(
            by_identity.values(),
            key=lambda hit: (-hit.message.received_at.timestamp(), hit.group_id, hit.message.message_id),
        ))
        artifact_by_id = {hit.artifact_id: hit for hit in (*approved_artifacts, *current_artifacts)}
        released_artifacts = tuple(sorted(
            artifact_by_id.values(),
            key=lambda hit: (-hit.created_at.timestamp(), hit.group_id, hit.artifact_id),
        ))
        release_order = sorted(
            [(hit.message.received_at.timestamp(), "message", hit.group_id, hit.message.message_id, hit)
             for hit in released_hits]
            + [(hit.created_at.timestamp(), "artifact", hit.group_id, hit.artifact_id, hit)
               for hit in released_artifacts],
            key=lambda item: (-item[0], item[2], item[3]),
        )[:query.limit]
        released_hits = tuple(item[4] for item in release_order if item[1] == "message")
        released_artifacts = tuple(item[4] for item in release_order if item[1] == "artifact")
        try:
            released_artifacts = await self._stage_approved_files(execution, released_artifacts)
        except (OSError, PersistentAgentError):
            return _safe_result(execution, "An approved file is no longer available for this Session.",
                                status="unavailable")
        if denied_source:
            if not (released_hits or released_artifacts):
                return _safe_result(
                    execution, "部分来源群未获批准或审批已过期，未返回其内容。", status="unavailable",
                )
            return base_result.model_copy(update={
                "hits": released_hits,
                "artifacts": released_artifacts,
                "status": "incomplete",
                "query_complete": False,
                "reason": "部分来源群未获批准或审批已过期，已省略这些内容。",
            })
        return base_result.model_copy(update={"hits": released_hits, "artifacts": released_artifacts})

    async def search_artifacts(self, execution, query, source_groups) -> tuple[tuple, bool]:
        """Search final files and indexed document links within the verified group scope."""
        selected = set(query.content_types) & {"files", "online_docs"}
        if not selected or query.cursor is not None:
            return (), True
        group_ids = tuple(sorted(item["group_id"] for item in source_groups))
        if not group_ids:
            return (), True
        try:
            rows = await asyncio.to_thread(
                self._sessions.search_session_artifacts,
                execution.namespace_id, execution.agent_id, group_ids, limit=10_000,
            )
        except Exception as exc:
            raise PersistentAgentError("Session artifact index is unavailable", code="ARTIFACT_SEARCH_UNAVAILABLE") from exc
        index_limit_reached = len(rows) == 10_000
        sources = {item["group_id"]: item for item in source_groups}
        terms = tuple(term.casefold() for term in query.search_terms)
        since = query.since.timestamp() if query.since is not None else None
        until = query.until.timestamp() if query.until is not None else None
        matches = []
        for row in rows:
            kind = row.get("artifact_kind")
            if (kind == "file" and "files" not in selected) or (kind == "online_doc" and "online_docs" not in selected):
                continue
            source = sources.get(row.get("source_group_id"))
            if source is None:
                continue
            try:
                created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
                visible_from = datetime.fromisoformat(str(source["visible_from"]).replace("Z", "+00:00"))
                stamp = created.timestamp()
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if stamp < visible_from.timestamp() or (since is not None and stamp < since) or (until is not None and stamp > until):
                continue
            searchable = " ".join(str(row.get(key) or "") for key in ("title", "file_name", "snippet", "url"))
            file_path = row.get("file_path")
            file_name = row.get("file_name")
            if kind == "file":
                if self._files is None or not all(isinstance(row.get(key), str) for key in ("file_path", "file_ref")):
                    continue
                if isinstance(row.get("file_size"), bool) or not isinstance(row.get("file_size"), int):
                    continue
                resolved = await asyncio.to_thread(
                    self._files.resolve, execution.namespace_id, execution.agent_id,
                    file_path, row["file_ref"], row["file_size"], expected_session_id=row.get("session_id"),
                )
                if resolved is None:
                    continue
                searchable += " " + await asyncio.to_thread(self._files.search_text, resolved, str(file_name or ""))
            folded = searchable.casefold()
            if terms and not any(term in folded for term in terms):
                continue
            snippet = str(row.get("snippet") or row.get("title") or "")
            if kind == "file" and terms and not any(term in snippet.casefold() for term in terms):
                searchable_folded = searchable.casefold()
                index = min((searchable_folded.find(term) for term in terms if term in searchable_folded), default=0)
                snippet = searchable[max(0, index - 100):index + 500]
            matches.append(core.GroupArtifactSearchHit(
                group_id=row["source_group_id"], group_name=source["name"], artifact_id=row["artifact_id"],
                platform=source.get("platform"), platform_chat_id=source.get("platform_chat_id"),
                artifact_kind=kind, title=row["title"], file_name=file_name,
                file_size=row.get("file_size"), file_digest=row.get("file_ref"),
                source_session_id=row.get("session_id") if kind == "file" else None,
                source_file_path=file_path if kind == "file" else None,
                url=row.get("url"), snippet=snippet[:4096] or None, created_at=created,
            ))
        matches.sort(key=lambda item: (-item.created_at.timestamp(), item.group_id, item.artifact_id))
        return tuple(matches[:query.limit + 1]), index_limit_reached or len(matches) > query.limit

    async def _stage_approved_files(self, execution, artifacts: tuple) -> tuple:
        staged = []
        for artifact in artifacts:
            if artifact.artifact_kind != "file":
                staged.append(artifact)
                continue
            if artifact.source_session_id == execution.session_id:
                imported_path = artifact.source_file_path
            else:
                if self._files is None:
                    raise PersistentAgentError("Session file import is unavailable", code="OUTPUT_FILE_UNAVAILABLE")
                imported_path = await asyncio.to_thread(
                    self._files.import_approved,
                    execution.namespace_id, execution.agent_id, execution.session_id, artifact.artifact_id,
                    artifact.source_file_path, artifact.file_digest, artifact.file_size,
                )
            staged.append(artifact.model_copy(update={"imported_path": imported_path}))
        return tuple(staged)

    async def _unchanged(self, namespace_id, result) -> bool:
        try:
            for hit in result.hits:
                messages = await asyncio.to_thread(
                    self._archive.current_messages, namespace_id, hit.group_id, (hit.message.message_id,),
                )
                if len(messages) != 1 or messages[0] != hit.message:
                    return False
            for artifact in result.artifacts:
                if artifact.artifact_kind == "file":
                    if self._files is None or self._files.resolve(
                        namespace_id, result.agent_id, artifact.source_file_path, artifact.file_digest,
                        artifact.file_size, expected_session_id=artifact.source_session_id,
                    ) is None:
                        return False
                else:
                    rows = await asyncio.to_thread(
                        self._sessions.search_session_artifacts, namespace_id, result.agent_id,
                        (artifact.group_id,), limit=10_000,
                    )
                    current = next((row for row in rows if row.get("artifact_id") == artifact.artifact_id), None)
                    if current is None or any(current.get(key) != getattr(artifact, attr) for key, attr in (
                        ("url", "url"), ("title", "title"), ("snippet", "snippet"),
                    )):
                        return False
                    try:
                        current_created = datetime.fromisoformat(str(current["created_at"]).replace("Z", "+00:00"))
                    except (KeyError, TypeError, ValueError):
                        return False
                    if current_created != artifact.created_at:
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
            if snapshot["result"].get("kind") == "cli_group_read":
                from jiuwenswarm.server.jiuwenmate.cli_group_snapshot import CliReadSnapshot
                from jiuwenswarm.server.jiuwenmate.cli_group_sharing import verify_cli_snapshot

                frozen = CliReadSnapshot.model_validate(snapshot["result"])
                if not await verify_cli_snapshot(self._cli, frozen):
                    return False
            else:
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
                required_types = (list(frozen.content_types) if snapshot["result"].get("kind") == "cli_group_read"
                                  else _snapshot_content_types(frozen))
                for source in row["source_groups"]:
                    if not await asyncio.to_thread(
                        self.repository.has_grant, row["scope"], source["group_id"], required_types,
                    ):
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

    async def sharing_sources(self, execution, query):
        """Host-only provenance for matched CLI reads; does not release candidate content."""
        return await self._delegate.sharing_sources(execution, query)

    async def prepare_tool_call(self, execution, tool_name: str, arguments: dict):
        from jiuwenswarm.server.jiuwenmate.session_artifact_sharing import SessionArtifactSharing

        return await SessionArtifactSharing(self._sharing, self._delegate).prepare(execution, tool_name, arguments)

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

    async def search_content(self, execution, query):
        """Combine selected message and artifact search before one approval per source group."""
        if query.cursor is not None and "messages" not in query.content_types:
            return _safe_result(execution, "Search cursors apply only to message results.", status="failed")
        try:
            before = await self._delegate.sharing_sources(execution, query)
        except Exception:
            return _safe_result(execution, "Current source group authorization is unavailable.", status="unavailable")

        message_result = None
        if "messages" in query.content_types:
            message_query = query.model_copy(update={"content_types": ("messages",)})
            message_result = await self._delegate.search_messages(execution, message_query)
            if message_result.status not in {"found", "empty", "incomplete"}:
                return message_result

        try:
            artifacts, artifact_more = await self._sharing.search_artifacts(execution, query, before)
        except PersistentAgentError:
            return _safe_result(execution, "Authorized Session artifact search is unavailable.", status="unavailable")

        try:
            sources = await self._delegate.sharing_sources(execution, query)
        except Exception:
            return _safe_result(execution, "Current source group authorization is unavailable.", status="unavailable")

        def authorization(items):
            return {item["group_id"]: {key: item[key] for key in ("app_id", "visible_from") if key in item}
                    for item in items}

        if authorization(before) != authorization(sources):
            return _safe_result(execution, "Source group authorization changed during search.", status="unavailable")

        hits = message_result.hits if message_result is not None else ()
        cursor = message_result.next_cursor if message_result is not None else None
        base_ref = (
            message_result.model_dump(mode="python", exclude={
                "status", "hits", "artifacts", "next_cursor", "query_complete", "reason",
            })
            if message_result is not None else core.InvocationRef.from_execution(execution).model_dump()
        )
        combined = sorted(
            [(hit.message.received_at.timestamp(), hit.group_id, hit.message.message_id, "message", hit) for hit in hits]
            + [(item.created_at.timestamp(), item.group_id, item.artifact_id, "artifact", item) for item in artifacts],
            key=lambda item: (-item[0], item[1], item[2]),
        )
        clipped = len(combined) > query.limit
        combined = combined[:query.limit]
        selected_hits = tuple(item[4] for item in combined if item[3] == "message")
        selected_artifacts = tuple(item[4] for item in combined if item[3] == "artifact")
        incomplete = artifact_more or clipped or (message_result is not None and message_result.status == "incomplete")
        if incomplete:
            reason = (
                message_result.reason if message_result is not None and message_result.status == "incomplete"
                else "More matching Session artifacts exist; narrow the search to review them."
                if artifact_more else "The search result reached its limit; narrow the query to continue."
            )
            status, complete = "incomplete", False
        elif selected_hits or selected_artifacts:
            reason, status, complete = None, "found", True
        else:
            reason, status, complete = None, "empty", True
        result = core.GroupMessageSearchResult(
            **base_ref, status=status, hits=selected_hits, artifacts=selected_artifacts,
            next_cursor=cursor, query_complete=complete, reason=reason,
        )
        return await self._sharing.authorize(execution, query, result, sources)

