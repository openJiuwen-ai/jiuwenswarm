"""Session-scoped Extractor/Builder scheduling and atomic publication."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

from .background_agents import BackgroundAgentRunner, ExtractorForkContext
from .evidence import (
    EvidenceWriter,
    read_json,
    resolve_evidence_blobs,
    utc_now,
    write_json_atomic,
)
from .memory_cli import DynamicMemoryGateway
from .prompts import BUILDER_SYSTEM_PROMPT, EXTRACTOR_SYSTEM_PROMPT, prompt_hashes
from .retrieval import constraint_candidates, extract_user_text, user_text_query


SNAPSHOT_LIMITS = {
    "resident_memory": 4,
    "recent_context": 4,
    "current_state": 6,
    "completed": 4,
    "next_actions": 4,
    "constraints": 6,
}
SNAPSHOT_ITEM_CHARACTER_LIMIT = 1000
SNAPSHOT_ITEM_RETRY_TARGET = 900

# A lagging worker must never turn many completed foreground tasks into one
# unbounded model request.  Four natural-task boundaries preserve throughput
# while keeping each extraction independently retryable and auditable.
MAX_TASKS_PER_EXTRACTION = 4
PROTOCOL_STRING_INLINE_LIMIT = 2048
PROTOCOL_CONTAINER_INLINE_LIMIT = 256 * 1024
FROZEN_WORKING_MEMORY_INLINE_LIMIT = 512 * 1024
UT_HASH_FIELDS = (
    "id",
    "memory_id",
    "priority",
    "content",
    "queries",
    "must_include",
    "evidence_refs",
    "source",
    "tags",
    "status",
)
EVIDENCE_RANGE_RE = re.compile(r"^raw-history:cursor-(\d+)-(\d+)$")


def _compact_protocol_value(value: Any) -> Any:
    """Bound protocol-heavy evidence without interpreting its semantics.

    Raw History remains byte-for-byte reconstructable.  The semantic Agent's
    view keeps the exact object shape and all small scalar metadata, while a
    large protocol string is represented by an exact digest and byte count.
    This prevents tool output, write payloads, and repeated model responses
    from consuming a model's entire context window.
    """
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) <= PROTOCOL_STRING_INLINE_LIMIT:
            return value
        return {
            "$raw_history_content": True,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "utf8_bytes": len(encoded),
        }
    if isinstance(value, dict):
        return {str(key): _compact_protocol_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_compact_protocol_value(item) for item in value]
    return value


def _compact_protocol_container(value: Any) -> Any:
    """Content-address an oversized aggregate while Raw History keeps it exact."""
    if not isinstance(value, (dict, list)):
        return _compact_protocol_value(value)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    if len(encoded) <= PROTOCOL_CONTAINER_INLINE_LIMIT:
        return value
    return {
        "$raw_history_container": True,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "utf8_bytes": len(encoded),
        "item_count": len(value),
    }


def _bound_frozen_working_memory(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep semantic boundary records plus a complete structural ledger when oversized."""
    encoded = json.dumps(
        events,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    if len(encoded) <= FROZEN_WORKING_MEMORY_INLINE_LIMIT:
        return events
    boundary_types = {
        "task-started",
        "user-message",
        "task-finished",
        "context-replaced",
    }
    boundary_events = [event for event in events if event.get("type") in boundary_types]
    ledger = [
        {
            "cursor": event.get("cursor"),
            "type": event.get("type"),
            "task_id": event.get("task_id"),
            "hash": event.get("hash"),
        }
        for event in events
    ]
    return [
        *boundary_events,
        {
            "type": "structural-event-ledger",
            "payload": {
                "event_count": len(events),
                "events": ledger,
                "compacted_view_sha256": hashlib.sha256(encoded).hexdigest(),
                "compacted_view_utf8_bytes": len(encoded),
                "complete_raw_history_path": "raw-history/events.jsonl",
            },
        },
    ]


def _tool_inventory(value: Any) -> list[str]:
    """Return tool identifiers only; schemas remain in the complete Raw History."""
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            function = item.get("function")
            name = str(
                item.get("name")
                or (function.get("name") if isinstance(function, dict) else "")
                or ""
            )
        else:
            name = str(getattr(item, "name", "") or "")
        if name and name not in names:
            names.append(name)
    return names


def _validate_extractor(
    value: dict[str, Any],
    *,
    candidate_constraints: list[dict[str, Any]] | None = None,
    direct_user_evidence: dict[str, str] | None = None,
) -> None:
    snapshot = value.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("extractor snapshot must be an object")
    for field, limit in SNAPSHOT_LIMITS.items():
        items = snapshot.get(field)
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise ValueError(f"snapshot.{field} must be a string list")
        if len(items) > limit:
            raise ValueError(
                f"snapshot.{field} has {len(items)} items; hard limit is {limit}. "
                f"Merge or remove {len(items) - limit} item(s)."
            )
        for index, item in enumerate(items):
            if len(item) > SNAPSHOT_ITEM_CHARACTER_LIMIT:
                raise ValueError(
                    f"snapshot.{field}[{index}] has {len(item)} characters; hard limit is "
                    f"{SNAPSHOT_ITEM_CHARACTER_LIMIT}. Rewrite that item to at most "
                    f"{SNAPSHOT_ITEM_RETRY_TARGET} characters without dropping its durable facts."
                )
    changes = value.get("changed_uts")
    if not isinstance(changes, list) or len(changes) > 4:
        raise ValueError("changed_uts must contain at most four items")
    for change_index, change in enumerate(changes):
        if not isinstance(change, dict):
            raise ValueError("each UT change must be an object")
        action = change.get("action", "upsert")
        if action not in {"upsert", "retire"}:
            raise ValueError("UT action must be upsert or retire")
        if action == "retire":
            if not str(change.get("id") or "").strip():
                raise ValueError("retire requires id")
            continue
        priority = change.get("priority")
        if isinstance(priority, bool) or priority not in {20, 40, 60, 80, 100}:
            raise ValueError("UT priority must be one of 20, 40, 60, 80, or 100")
        content = change.get("content")
        queries = change.get("queries")
        must_include = change.get("must_include")
        if not isinstance(content, str) or not content.strip() or len(content) > 700:
            raise ValueError("UT content must be a non-empty string of at most 700 characters")
        if not isinstance(queries, list) or not queries or len(queries) > 4:
            raise ValueError("UT queries must contain one to four items")
        if not isinstance(must_include, list) or not must_include or len(must_include) > 3:
            raise ValueError("UT must_include must contain one to three items")
        for phrase_index, phrase in enumerate(must_include):
            if not isinstance(phrase, str) or phrase not in content:
                ut_id = str(change.get("id") or "<missing-id>")
                raise ValueError(
                    f"changed_uts[{change_index}] id={ut_id!r} must_include"
                    f"[{phrase_index}]={phrase!r} is not an exact substring of content; "
                    "copy it verbatim into content or replace it with an exact, "
                    "answer-bearing substring already present in content"
                )
    candidates = candidate_constraints or []
    if not candidates:
        return
    assessments = value.get("constraint_assessments")
    if not isinstance(assessments, list):
        raise ValueError("constraint_assessments must cover every candidate constraint")
    by_id: dict[str, dict[str, Any]] = {}
    for assessment in assessments:
        if not isinstance(assessment, dict):
            raise ValueError("each constraint assessment must be an object")
        # ``id`` is a harmless structural alias that models commonly copy from
        # the candidate object itself.  Canonicalize it here because the
        # Harness owns structure, while keeping conflicting identifiers
        # fail-closed so no semantic association can be guessed.
        raw_ut_id = assessment.get("ut_id")
        raw_alias = assessment.get("id")
        if raw_ut_id in (None, "") and raw_alias not in (None, ""):
            assessment["ut_id"] = raw_alias
            raw_ut_id = raw_alias
        elif (
            raw_ut_id not in (None, "")
            and raw_alias not in (None, "")
            and str(raw_ut_id).strip() != str(raw_alias).strip()
        ):
            raise ValueError("constraint assessment id and ut_id must match")
        assessment.pop("id", None)
        ut_id = str(raw_ut_id or "").strip()
        if not ut_id or ut_id in by_id:
            raise ValueError("constraint assessments require unique non-empty ut_id values")
        outcome = assessment.get("outcome")
        if outcome not in {"preserved", "unresolved", "overridden", "not_relevant"}:
            raise ValueError(f"constraint assessment {ut_id!r} has invalid outcome")
        by_id[ut_id] = assessment
    expected = {str(item.get("id")) for item in candidates}
    if set(by_id) != expected:
        raise ValueError(
            "constraint_assessments must exactly cover candidate constraints: "
            f"expected={sorted(expected)!r}, actual={sorted(by_id)!r}"
        )
    snapshot_constraints = snapshot.get("constraints") or []
    user_evidence = direct_user_evidence or {}
    for candidate in candidates:
        ut_id = str(candidate.get("id"))
        assessment = by_id[ut_id]
        outcome = assessment["outcome"]
        if outcome == "unresolved":
            notice = assessment.get("snapshot_notice")
            if not isinstance(notice, str) or notice not in snapshot_constraints:
                raise ValueError(
                    f"unresolved constraint {ut_id!r} requires snapshot_notice copied "
                    "verbatim into snapshot.constraints"
                )
        if outcome != "overridden":
            continue
        refs = assessment.get("direct_user_evidence_refs")
        quotes = assessment.get("acknowledgement_quotes")
        if not isinstance(refs, list) or not refs or not all(ref in user_evidence for ref in refs):
            raise ValueError(
                f"overridden constraint {ut_id!r} requires direct user evidence references"
            )
        if not isinstance(quotes, list) or not quotes or not all(
            isinstance(quote, str)
            and quote
            and any(quote in user_evidence[ref] for ref in refs)
            for quote in quotes
        ):
            raise ValueError(
                f"overridden constraint {ut_id!r} requires exact acknowledgement quotes"
            )
        anchors = [
            str(item).casefold()
            for item in candidate.get("must_include") or []
            if str(item).strip()
        ]
        if anchors and not any(
            anchor in quote.casefold() for anchor in anchors for quote in quotes
        ):
            raise ValueError(
                f"overridden constraint {ut_id!r} acknowledgement must name an exact "
                "constraint anchor"
            )


def _validate_builder(value: dict[str, Any]) -> None:
    if not isinstance(value.get("approved"), bool):
        raise ValueError("builder approved must be boolean")
    if not isinstance(value.get("diagnostics"), list):
        raise ValueError("builder diagnostics must be a list")


def _normalize_changes(
    changes: list[dict[str, Any]], session_id: str, evidence_ref: str
) -> list[dict[str, Any]]:
    """Fill structural ownership fields without changing Agent-authored semantics."""
    normalized: list[dict[str, Any]] = []
    for item in changes:
        value = dict(item)
        if value.get("action", "upsert") == "upsert":
            value["action"] = "upsert"
            value["source"] = session_id
            value["evidence_refs"] = [evidence_ref]
            value.setdefault("tags", [])
            if not str(value.get("memory_id") or "").strip():
                value["memory_id"] = f"memory-{value.get('id')}"
        normalized.append(value)
    return normalized


def _raw_range(
    path: Path,
    start: int,
    end: int,
    *,
    max_finished_tasks: int = MAX_TASKS_PER_EXTRACTION,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    finished_tasks = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                event = resolve_evidence_blobs(json.loads(line), path.parent)
                cursor = int(event.get("cursor") or 0)
                if start <= cursor <= end:
                    result.append(event)
                    if event.get("type") == "task-finished":
                        finished_tasks += 1
                        if finished_tasks >= max_finished_tasks:
                            break
    except FileNotFoundError:
        pass
    return result


def _extractor_evidence(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a non-semantic, bounded view over a frozen Raw History range.

    Every model-visible envelope contains the whole accumulated message list,
    so copying every envelope into the Extractor prompt repeats the same
    context many times.  Raw History remains untouched.  The Extractor gets
    the latest exact Final Visible Context, every non-envelope event, and an
    envelope ledger retaining each response/status/usage/hash.  This follows
    the architecture's semantic-view/evidence-view split without asking the
    Harness to decide what should be remembered.
    """
    compact_events: list[dict[str, Any]] = []
    latest_visible: dict[str, Any] | None = None
    envelope_count = 0
    for event in events:
        if event.get("type") != "model-visible-envelope":
            if event.get("type") in {"tool-call", "tool-result"}:
                compact = dict(event)
                compact["payload"] = _compact_protocol_value(event.get("payload"))
                compact_events.append(compact)
            elif event.get("type") == "context-replaced":
                compact = dict(event)
                payload = dict(event.get("payload") or {})
                payload["replaced_messages"] = _compact_protocol_container(
                    payload.get("replaced_messages")
                )
                compact["payload"] = payload
                compact_events.append(compact)
            elif event.get("type") == "task-finished":
                compact = dict(event)
                compact["payload"] = _compact_protocol_value(event.get("payload"))
                compact_events.append(compact)
            else:
                compact_events.append(event)
            continue
        envelope_count += 1
        payload = dict(event.get("payload") or {})
        latest_visible = {
            "cursor": event.get("cursor"),
            "hash": event.get("hash"),
            "task_id": event.get("task_id"),
            "created_at": event.get("created_at"),
            "messages": _compact_protocol_container(payload.get("messages")),
            "tools": _tool_inventory(payload.get("tools")),
            "status": payload.get("status"),
        }
        compact_events.append(
            {
                "cursor": event.get("cursor"),
                "type": event.get("type"),
                "session_id": event.get("session_id"),
                "task_id": event.get("task_id"),
                "created_at": event.get("created_at"),
                "previous_hash": event.get("previous_hash"),
                "hash": event.get("hash"),
                "payload": {
                    "status": payload.get("status"),
                    "response": _compact_protocol_value(payload.get("response")),
                    "usage": payload.get("usage"),
                    "exception": payload.get("exception"),
                    "visible_context_cursor": event.get("cursor"),
                },
            }
        )
    return {
        "final_visible_context": latest_visible,
        "frozen_working_memory": _bound_frozen_working_memory(compact_events),
        "raw_history_manifest": {
            "from_cursor": events[0].get("cursor") if events else None,
            "to_cursor": events[-1].get("cursor") if events else None,
            "event_count": len(events),
            "model_visible_envelope_count": envelope_count,
            "first_hash": events[0].get("hash") if events else None,
            "last_hash": events[-1].get("hash") if events else None,
            "complete_raw_history_path": "raw-history/events.jsonl",
        },
    }


def _memory_query(events: list[dict[str, Any]]) -> str:
    return user_text_query(events)


def _direct_user_evidence(events: list[dict[str, Any]]) -> dict[str, str]:
    """Index exact direct-user messages by cursor for structural validation."""
    evidence: dict[str, str] = {}
    for event in events:
        if event.get("type") != "user-message":
            continue
        payload = event.get("payload")
        candidate = payload.get("parts") if isinstance(payload, dict) else payload
        text = extract_user_text(candidate)
        cursor = int(event.get("cursor") or 0)
        if cursor > 0 and text:
            evidence[f"raw-history:cursor-{cursor}"] = text
    return evidence


def _ut_content_hash(record: dict[str, Any]) -> str:
    semantic = {key: record.get(key) for key in UT_HASH_FIELDS}
    encoded = json.dumps(
        semantic,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SessionCoordinator:
    """Own all mutable eternal state for exactly one product Session."""

    def __init__(
        self,
        root: Path,
        session_id: str,
        model_supplier: Callable[[], Any],
    ) -> None:
        self.root = root
        self.session_id = session_id
        self.evidence = EvidenceWriter(root, session_id)
        self.memory = DynamicMemoryGateway(root / "memory", self.evidence)
        self.agents = BackgroundAgentRunner(
            model_supplier,
            self.evidence,
            root=root,
            session_id=session_id,
        )
        self.state_path = root / "state" / "harness.json"
        self.projection_path = root / "state" / "eternal-conversation.json"
        self._worker: asyncio.Task[None] | None = None
        self._builder: asyncio.Task[None] | None = None
        self._extractor_forks: dict[int, ExtractorForkContext] = {}
        self._closed = False
        self._schedule_lock = asyncio.Lock()
        # Publish/freeze/copy/build are short formal-state transitions.  The
        # Agents run outside this lock, so foreground extraction can continue,
        # while every Builder staging copy is still a consistent Pending view.
        self._memory_publication_lock = asyncio.Lock()
        self._write_manifest()

    def _write_manifest(self) -> None:
        path = self.root / "audit" / "source-manifest.json"
        if path.exists():
            return
        write_json_atomic(
            path,
            {
                "implementation": "jiuwenswarm.persist-session",
                "schema_version": 1,
                "prompt_hashes": prompt_hashes(),
            },
        )

    async def request_extract(
        self,
        cursor: int,
        *,
        fork_context: ExtractorForkContext | None = None,
    ) -> None:
        if self._closed:
            return
        if fork_context is not None:
            await self.evidence.append_audit(
                "extractor-forks",
                {
                    "cursor": int(cursor),
                    **fork_context.manifest(),
                    "accepted": fork_context.within_seventy_percent,
                    "limit": "70% of model context window",
                },
            )
            if fork_context.within_seventy_percent:
                self._extractor_forks[int(cursor)] = fork_context
        async with self._schedule_lock:
            state = read_json(self.state_path, {}) or {}
            requested = max(int(state.get("requested_cursor") or 0), int(cursor))
            state["requested_cursor"] = requested
            state["updated_at"] = utc_now()
            state.pop("extractor_error", None)
            await asyncio.to_thread(write_json_atomic, self.state_path, state)
            if self._worker is None or self._worker.done():
                # A clean Context prevents foreground request/session bindings
                # from leaking into either background Agent.
                clean = contextvars.Context()
                self._worker = asyncio.create_task(
                    self._guarded_extract_loop(),
                    name=f"eternal-extractor:{self.session_id}",
                    context=clean,
                )

    async def resume_background(self) -> None:
        """Resume durable cursor/Pending work after an Adapter or process restart."""
        state = read_json(self.state_path, {}) or {}
        requested = int(state.get("requested_cursor") or 0)
        if requested:
            # Starting a new durable retry boundary supersedes a failure from
            # the previous process/request. Clear it synchronously before the
            # extractor task is scheduled: otherwise an observer can see the
            # stale builder failure and fail fast while the retry is already
            # queued but has not reached _schedule_builder() yet.
            await self._clear_worker_error("builder")
            await self.request_extract(requested)
        elif (self.root / "memory" / "memory.sqlite3").exists():
            await self._schedule_builder()

    async def _guarded_extract_loop(self) -> None:
        try:
            await self._extract_loop()
        except Exception as exc:
            await self.evidence.append_agent_history(
                "extractor",
                {"status": "error", "error_type": type(exc).__name__, "error": str(exc)},
            )
            await self._record_worker_error("extractor", exc)

    async def _extract_loop(self) -> None:
        await self.memory.ensure_initialized()
        while not self._closed:
            requested = int((read_json(self.state_path, {}) or {}).get("requested_cursor") or 0)
            formal = await self.memory.projection()
            covered = int(formal.get("covered_through") or 0)
            if requested <= covered:
                await self._schedule_builder()
                return
            events = await asyncio.to_thread(
                _raw_range, self.evidence.raw_path, covered + 1, requested
            )
            if not events or int(events[0].get("cursor") or 0) != covered + 1:
                raise RuntimeError("raw history range is incomplete; refusing publication")
            target = int(events[-1].get("cursor") or 0)
            if target > requested or events[-1].get("type") != "task-finished":
                raise RuntimeError("raw history batch has no complete task boundary")
            published = await self.memory.call("list", "--full")
            memories = list(published.get("memories") or [])
            related = await self.memory.search(_memory_query(events)) if memories else {"matches": []}
            related_ids = [str(item.get("id")) for item in related.get("matches") or []][:32]
            by_id = {str(item.get("id")): item for item in memories}
            related_records = [by_id[item_id] for item_id in related_ids if item_id in by_id]
            candidates = constraint_candidates(related_records)
            user_evidence = _direct_user_evidence(events)
            high_priority = sorted(memories, key=lambda item: int(item.get("priority") or 0), reverse=True)[:12]
            references: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in [*(by_id[item_id] for item_id in related_ids if item_id in by_id), *high_priority]:
                item_id = str(item.get("id"))
                if item_id not in seen:
                    seen.add(item_id)
                    references.append(item)
            evidence_ref = f"raw-history:cursor-{covered + 1}-{target}"
            evidence_view = _extractor_evidence(events)
            request = {
                "old_snapshot": formal.get("snapshot") or {},
                **evidence_view,
                "published_uts": references,
                "candidate_constraints": candidates,
                "direct_user_evidence": user_evidence,
                "source": self.session_id,
                "evidence_ref": evidence_ref,
            }
            parsed = await self.agents.call_json(
                role="extractor",
                system_prompt=EXTRACTOR_SYSTEM_PROMPT,
                request=request,
                validate=lambda value: _validate_extractor(
                    value,
                    candidate_constraints=candidates,
                    direct_user_evidence=user_evidence,
                ),
                fork_context=self._extractor_forks.get(target),
            )
            proposal = {
                "base_memory_revision": formal["memory_revision"],
                "base_snapshot_revision": formal["snapshot_revision"],
                "from_cursor": covered + 1,
                "to_cursor": target,
                "snapshot": parsed["snapshot"],
                "changed_uts": _normalize_changes(
                    list(parsed.get("changed_uts") or []), self.session_id, evidence_ref
                ),
                "evidence_refs": [evidence_ref],
                "semantic_statement": parsed.get("semantic_statement")
                or "All future-relevant effects are carried.",
            }
            async with self._memory_publication_lock:
                result = await self.memory.file_command(
                    "publish-pending", proposal, f"proposal-{covered + 1}-{target}"
                )
            await asyncio.to_thread(write_json_atomic, self.projection_path, result)
            await self.evidence.append_audit(
                "publications", {"proposal": proposal, "result": result}
            )
            for cursor in [value for value in self._extractor_forks if value <= target]:
                self._extractor_forks.pop(cursor, None)
            await self._schedule_builder()

    async def _schedule_builder(self) -> None:
        if self._builder is None or self._builder.done():
            await self._clear_worker_error("builder")
            self._builder = asyncio.create_task(
                self._guarded_build_loop(),
                name=f"eternal-builder:{self.session_id}",
                context=contextvars.Context(),
            )

    async def _guarded_build_loop(self) -> None:
        try:
            await self._build_loop()
        except Exception as exc:
            await self.evidence.append_agent_history(
                "builder",
                {"status": "error", "error_type": type(exc).__name__, "error": str(exc)},
            )
            await self._record_worker_error("builder", exc)

    async def _clear_worker_error(self, worker: str) -> None:
        async with self._schedule_lock:
            state = read_json(self.state_path, {}) or {}
            if state.pop(f"{worker}_error", None) is None:
                return
            state["updated_at"] = utc_now()
            await asyncio.to_thread(write_json_atomic, self.state_path, state)

    async def _record_worker_error(self, worker: str, exc: Exception) -> None:
        async with self._schedule_lock:
            state = read_json(self.state_path, {}) or {}
            state[f"{worker}_error"] = {
                "at": utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            state["updated_at"] = utc_now()
            await asyncio.to_thread(write_json_atomic, self.state_path, state)

    async def _build_loop(self) -> None:
        while not self._closed:
            output = self.root / "memory" / "jobs" / f"build-{utc_now().replace(':', '-')}.json"
            async with self._memory_publication_lock:
                frozen = await self.memory.call("freeze-pending", "--output", str(output))
                if int(frozen.get("count") or 0) == 0:
                    return
                batch = read_json(output, {}) or {}
                builder_request: dict[str, Any] = dict(batch)
                staging: dict[str, Any] | None = None
                if self.agents.uses_deep_agent():
                    staging = await self._prepare_builder_staging(output, batch)
                    builder_request = {
                        "frozen_batch": batch,
                        "builder_run": {
                            "workspace": str(staging["workspace"]),
                            "memory_root": staging["memory_root"],
                            "batch": staging["batch"],
                            "manifest": staging["manifest"],
                            "required_commands": [
                                "build-pending",
                                "test --built-only",
                                "bench",
                            ],
                        },
                    }
            review = await self.agents.call_json(
                role="builder",
                system_prompt=BUILDER_SYSTEM_PROMPT,
                request=builder_request,
                validate=_validate_builder,
            )
            if not review["approved"]:
                await self.evidence.append_audit("builds", {"batch": str(output), "review": review})
                diagnostics = "; ".join(str(item) for item in review.get("diagnostics") or [])
                raise RuntimeError(
                    "builder rejected frozen Pending batch"
                    + (f": {diagnostics}" if diagnostics else "")
                )
            staging_validation = None
            if staging is not None:
                staging_validation = await self._validate_builder_staging(staging, batch)
            async with self._memory_publication_lock:
                await self._validate_builder_publication(batch)
                result = await self.memory.call("build-pending", "--file", str(output))
            await self.evidence.append_audit(
                "builds",
                {
                    "batch": str(output),
                    "review": review,
                    "staging": staging_validation,
                    "result": result,
                },
            )

    async def _prepare_builder_staging(
        self, frozen_path: Path, batch: dict[str, Any]
    ) -> dict[str, Any]:
        """Copy canonical Pending into a unique Builder-only staging project."""
        workspace = self.agents.prepare_workspace("builder")
        run_id = f"build-{int(batch.get('memory_revision') or 0)}-{uuid.uuid4().hex[:12]}"
        run_root = workspace / "runs" / run_id
        memory_root = run_root / "memory"
        batch_path = run_root / "pending-batch.json"
        manifest_path = run_root / "build-manifest.json"

        def _copy() -> None:
            run_root.mkdir(parents=True, exist_ok=False)
            shutil.copytree(
                self.root / "memory",
                memory_root,
                ignore=shutil.ignore_patterns("jobs"),
            )
            shutil.copy2(frozen_path, batch_path)

        await asyncio.to_thread(_copy)
        return {
            "workspace": workspace,
            "run_root": run_root,
            "memory_root": memory_root.relative_to(workspace).as_posix(),
            "batch": batch_path.relative_to(workspace).as_posix(),
            "manifest": manifest_path.relative_to(workspace).as_posix(),
        }

    async def _validate_builder_staging(
        self, staging: dict[str, Any], batch: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate Builder command evidence and staged Built state structurally."""
        workspace = Path(staging["workspace"])
        manifest_path = workspace / str(staging["manifest"])
        manifest = read_json(manifest_path, {}) or {}
        commands = manifest.get("commands")
        if manifest.get("success") is not True or not isinstance(commands, list):
            raise RuntimeError("Builder did not produce a successful build manifest")
        required = (("build-pending",), ("test", "--built-only"), ("bench",))
        if len(commands) != len(required):
            raise RuntimeError("Builder manifest does not contain all required memory-cli calls")
        for record, required_tokens in zip(commands, required):
            command = record.get("command") if isinstance(record, dict) else None
            if (
                not isinstance(command, list)
                or int(record.get("returncode", -1)) != 0
                or not all(token in command for token in required_tokens)
            ):
                raise RuntimeError(
                    f"Builder memory-cli evidence failed for {' '.join(required_tokens)}"
                )
        manifest_batch = Path(str(manifest.get("batch") or "")).as_posix()
        expected_batch = Path(str(staging["batch"])).as_posix()
        if manifest_batch != expected_batch:
            raise RuntimeError("Builder manifest batch path does not match the frozen input")

        staging_gateway = DynamicMemoryGateway(
            workspace / str(staging["memory_root"]),
            self.evidence,
            script=workspace
            / "skills"
            / "dynamic-memory-cli"
            / "scripts"
            / "dynamic_memory_cli.py",
        )
        listed = await staging_gateway.call("list", "--full")
        by_id = {
            str(item.get("id")): item for item in list(listed.get("memories") or [])
        }
        expected_ids = [
            str(item.get("id"))
            for item in list(batch.get("items") or [])
            if str(item.get("id") or "")
        ]
        missing = [
            item_id
            for item_id in expected_ids
            if str((by_id.get(item_id) or {}).get("build_state") or "") != "built"
        ]
        if missing:
            raise RuntimeError(f"Builder staging did not build frozen UTs: {missing}")
        return {
            "workspace": str(workspace),
            "run_root": str(staging["run_root"]),
            "manifest": manifest,
            "built_ids": expected_ids,
        }

    async def _validate_builder_publication(self, batch: dict[str, Any]) -> None:
        """Recheck frozen revision/cursor/hash/evidence immediately before Built."""
        for field in ("memory_revision", "snapshot_revision", "covered_through"):
            if not isinstance(batch.get(field), int) or int(batch[field]) < 0:
                raise RuntimeError(f"Builder frozen batch has invalid {field}")
        formal = await self.memory.projection()
        for field in ("memory_revision", "snapshot_revision", "covered_through"):
            if int(formal.get(field) or 0) < int(batch[field]):
                raise RuntimeError(f"canonical {field} moved behind the frozen Builder batch")

        required_cursors: set[int] = set()
        covered = int(batch["covered_through"])
        for item in list(batch.get("items") or []):
            if not isinstance(item, dict):
                raise RuntimeError("Builder frozen batch contains a non-object UT")
            if item.get("content_hash") != _ut_content_hash(item):
                raise RuntimeError(f"Builder frozen UT hash mismatch: {item.get('id')}")
            evidence_refs = item.get("evidence_refs")
            if not isinstance(evidence_refs, list) or not evidence_refs:
                raise RuntimeError(f"Builder frozen UT has no evidence: {item.get('id')}")
            for reference in evidence_refs:
                match = EVIDENCE_RANGE_RE.fullmatch(str(reference))
                if match is None:
                    raise RuntimeError(f"Builder frozen UT has invalid evidence: {item.get('id')}")
                start, end = (int(value) for value in match.groups())
                if start < 1 or end < start or end > covered:
                    raise RuntimeError(f"Builder frozen UT evidence is out of range: {item.get('id')}")
                required_cursors.update((start, end))

        if required_cursors:
            found: set[int] = set()

            def _scan_raw_history() -> None:
                try:
                    with self.evidence.raw_path.open("r", encoding="utf-8") as handle:
                        for line in handle:
                            if not line.strip():
                                continue
                            cursor = int(json.loads(line).get("cursor") or 0)
                            if cursor in required_cursors:
                                found.add(cursor)
                except FileNotFoundError:
                    return

            await asyncio.to_thread(_scan_raw_history)
            missing = sorted(required_cursors - found)
            if missing:
                raise RuntimeError(f"Builder frozen UT evidence cursors are missing: {missing}")

    async def projection_for_boundary(self, *, force: bool = False) -> dict[str, Any] | None:
        """Return a projection only when no newer foreground task can be lost."""
        if not (self.root / "memory" / "memory.sqlite3").exists():
            return None
        formal = await self.memory.projection()
        requested = int((read_json(self.state_path, {}) or {}).get("requested_cursor") or 0)
        revision = int(formal.get("snapshot_revision") or 0)
        state = read_json(self.state_path, {}) or {}
        applied = int(state.get("applied_snapshot_revision") or 0)
        if (revision <= applied and not force) or int(formal.get("covered_through") or 0) != requested:
            return None
        return formal

    async def wait_for_projection_boundary(self) -> dict[str, Any] | None:
        """Wait only for the Extractor when foreground replacement needs headroom.

        Pending is already published long-term memory, so foreground replacement
        does not need to wait for the Builder.  The guarded Extractor task records
        its own failure and then finishes; in that case no unsafe projection is
        returned and the normal context-processing path remains in control.
        """
        worker = self._worker
        if worker is not None and not worker.done():
            await asyncio.shield(worker)
        return await self.projection_for_boundary(force=True)

    async def mark_projection_applied(self, revision: int) -> None:
        async with self._schedule_lock:
            state = read_json(self.state_path, {}) or {}
            state["applied_snapshot_revision"] = int(revision)
            state["updated_at"] = utc_now()
            await asyncio.to_thread(write_json_atomic, self.state_path, state)

    async def wait_idle(self) -> None:
        while True:
            active = [task for task in (self._worker, self._builder) if task is not None and not task.done()]
            if not active:
                return
            await asyncio.gather(*active, return_exceptions=True)

    async def close(self) -> None:
        await self.wait_idle()
        self._closed = True
        await self.agents.close()


__all__ = ["SessionCoordinator"]
