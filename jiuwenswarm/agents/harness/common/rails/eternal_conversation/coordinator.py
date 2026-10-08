"""Session-scoped Extractor/Builder scheduling and atomic publication."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .background_agents import BackgroundAgentRunner
from .evidence import (
    EvidenceWriter,
    read_json,
    resolve_evidence_blobs,
    utc_now,
    write_json_atomic,
)
from .memory_cli import DynamicMemoryGateway
from .prompts import BUILDER_SYSTEM_PROMPT, EXTRACTOR_SYSTEM_PROMPT, prompt_hashes


SNAPSHOT_LIMITS = {
    "resident_memory": 4,
    "recent_context": 4,
    "current_state": 6,
    "completed": 4,
    "next_actions": 4,
    "constraints": 6,
}

# A lagging worker must never turn many completed foreground tasks into one
# unbounded model request.  Four natural-task boundaries preserve throughput
# while keeping each extraction independently retryable and auditable.
MAX_TASKS_PER_EXTRACTION = 4
PROTOCOL_STRING_INLINE_LIMIT = 2048


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


def _validate_extractor(value: dict[str, Any]) -> None:
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
            if len(item) > 280:
                raise ValueError(
                    f"snapshot.{field}[{index}] has {len(item)} characters; hard limit is 280. "
                    "Rewrite that item to at most 220 characters without dropping its durable facts."
                )
    changes = value.get("changed_uts")
    if not isinstance(changes, list) or len(changes) > 4:
        raise ValueError("changed_uts must contain at most four items")
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("each UT change must be an object")
        action = change.get("action", "upsert")
        if action not in {"upsert", "retire"}:
            raise ValueError("UT action must be upsert or retire")
        if action == "retire":
            if not str(change.get("id") or "").strip():
                raise ValueError("retire requires id")
            continue
        content = change.get("content")
        queries = change.get("queries")
        must_include = change.get("must_include")
        if not isinstance(content, str) or not content.strip() or len(content) > 700:
            raise ValueError("UT content must be a non-empty string of at most 700 characters")
        if not isinstance(queries, list) or not queries or len(queries) > 4:
            raise ValueError("UT queries must contain one to four items")
        if not isinstance(must_include, list) or not must_include or len(must_include) > 3:
            raise ValueError("UT must_include must contain one to three items")
        for phrase in must_include:
            if not isinstance(phrase, str) or phrase not in content:
                raise ValueError("every must_include phrase must be an exact content substring")


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
            value.setdefault("memory_id", f"memory-{value.get('id')}")
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
            "messages": payload.get("messages"),
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
        "frozen_working_memory": compact_events,
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
    parts: list[str] = []
    for event in reversed(events):
        if event.get("type") not in {"user-message", "task-started", "model-visible-envelope"}:
            continue
        payload = event.get("payload")
        text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
        if text.strip():
            parts.append(text[:800])
        if len(parts) == 4:
            break
    return " ".join(reversed(parts))[:2400] or "recent conversation"


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
        self.agents = BackgroundAgentRunner(model_supplier, self.evidence)
        self.state_path = root / "state" / "harness.json"
        self.projection_path = root / "state" / "eternal-conversation.json"
        self._worker: asyncio.Task[None] | None = None
        self._builder: asyncio.Task[None] | None = None
        self._closed = False
        self._schedule_lock = asyncio.Lock()
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

    async def request_extract(self, cursor: int) -> None:
        if self._closed:
            return
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
                "source": self.session_id,
                "evidence_ref": evidence_ref,
            }
            parsed = await self.agents.call_json(
                role="extractor",
                system_prompt=EXTRACTOR_SYSTEM_PROMPT,
                request=request,
                validate=_validate_extractor,
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
            result = await self.memory.file_command(
                "publish-pending", proposal, f"proposal-{covered + 1}-{target}"
            )
            await asyncio.to_thread(write_json_atomic, self.projection_path, result)
            await self.evidence.append_audit(
                "publications", {"proposal": proposal, "result": result}
            )
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
            frozen = await self.memory.call("freeze-pending", "--output", str(output))
            if int(frozen.get("count") or 0) == 0:
                return
            batch = read_json(output, {}) or {}
            review = await self.agents.call_json(
                role="builder",
                system_prompt=BUILDER_SYSTEM_PROMPT,
                request=batch,
                validate=_validate_builder,
            )
            if not review["approved"]:
                await self.evidence.append_audit("builds", {"batch": str(output), "review": review})
                diagnostics = "; ".join(str(item) for item in review.get("diagnostics") or [])
                raise RuntimeError(
                    "builder rejected frozen Pending batch"
                    + (f": {diagnostics}" if diagnostics else "")
                )
            result = await self.memory.call("build-pending", "--file", str(output))
            await self.evidence.append_audit(
                "builds", {"batch": str(output), "review": review, "result": result}
            )

    async def projection_for_boundary(self) -> dict[str, Any] | None:
        """Return a projection only when no newer foreground task can be lost."""
        if not (self.root / "memory" / "memory.sqlite3").exists():
            return None
        formal = await self.memory.projection()
        requested = int((read_json(self.state_path, {}) or {}).get("requested_cursor") or 0)
        revision = int(formal.get("snapshot_revision") or 0)
        state = read_json(self.state_path, {}) or {}
        applied = int(state.get("applied_snapshot_revision") or 0)
        if revision <= applied or int(formal.get("covered_through") or 0) != requested:
            return None
        return formal

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


__all__ = ["SessionCoordinator"]
