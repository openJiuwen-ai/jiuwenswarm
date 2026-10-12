# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Durable, workspace-scoped file mutation records for Code turns."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_agent_root_dir

_INDEX_VERSION = 1
_MAX_CAPTURE_BYTES = 5 * 1024 * 1024
_MUTATING_TOOL_NAMES = frozenset(
    {"edit", "edit_file", "write", "write_file", "apply_patch"}
)
_REJECTED_PARTS = frozenset({".git", ".jiuwenswarm", ".agent_history"})


@dataclass(frozen=True, slots=True)
class PendingCapture:
    """Workspace state sampled immediately before one mutating tool call."""

    tool_name: str
    path: str
    call_id: str | None
    before_hash: str | None
    before_snapshot: str | None


@dataclass(slots=True)
class FileMutation:
    """The first-before and latest-after states for one file in a turn."""

    path: str
    kind: str
    before_hash: str | None
    after_hash: str | None
    before_snapshot: str | None
    after_snapshot: str | None
    call_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TurnRecord:
    """One finalized (or currently active) turn mutation record."""

    session_id: str
    turn_id: str
    task_text: str
    revision: int
    mutations: list[FileMutation] = field(default_factory=list)
    started_at: str = ""
    finalized_at: str | None = None


class TurnMutationStore:
    """Persist per-turn mutation snapshots outside a source workspace."""

    def __init__(self, root: Path, workspace: Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.index_path = self.root / "index.json"
        self.snapshot_root = self.root / "snapshots"
        data = self._read_index()

        indexed_workspace = data.get("workspace")
        if workspace is None:
            if not indexed_workspace:
                raise ValueError("workspace is required for a new mutation store")
            workspace = Path(str(indexed_workspace))
        self.workspace = Path(workspace).expanduser().resolve()
        if indexed_workspace:
            persisted = Path(str(indexed_workspace)).expanduser().resolve()
            if persisted != self.workspace:
                raise ValueError("workspace does not match the persisted index")

        self._turns = [self._turn_from_dict(item) for item in data.get("turns", [])]
        self._active: TurnRecord | None = None

    def begin_turn(
        self,
        session_id: str,
        task_text: str = "",
        turn_id: str | None = None,
    ) -> TurnRecord:
        """Begin a turn; callers must finalize it before beginning another."""

        if self._active is not None:
            raise RuntimeError("a mutation turn is already active")
        identifier = str(turn_id or uuid.uuid4().hex)
        if any(turn.turn_id == identifier for turn in self._turns):
            raise ValueError(f"turn already exists: {identifier}")
        next_revision = max((turn.revision for turn in self._turns), default=0) + 1
        self._active = TurnRecord(
            session_id=str(session_id),
            turn_id=identifier,
            task_text=str(task_text or ""),
            revision=next_revision,
            started_at=_utc_now(),
        )
        return self._active

    def capture_before(
        self,
        tool_name: str,
        args: Any,
        call_id: str | None = None,
    ) -> PendingCapture | None:
        """Capture a safe edit/write target before a tool is invoked."""

        if self._active is None:
            return None
        normalized_tool = str(tool_name or "").rsplit(".", 1)[-1].lower()
        if normalized_tool not in _MUTATING_TOOL_NAMES:
            return None
        raw_path = _extract_path(args)
        if not raw_path:
            return None
        target = self._resolve_workspace_path(raw_path)
        if target is None or self._is_rejected_path(target):
            return None
        before = self._read_file(target)
        if before is _TOO_LARGE:
            return None
        before_bytes = before if isinstance(before, bytes) else None
        before_hash = _sha256(before_bytes)
        before_snapshot = self._save_snapshot(before_bytes)
        return PendingCapture(
            tool_name=str(tool_name),
            path=target.relative_to(self.workspace).as_posix(),
            call_id=str(call_id) if call_id is not None else None,
            before_hash=before_hash,
            before_snapshot=before_snapshot,
        )

    def commit_after(
        self,
        pending: PendingCapture | None,
        applied: bool = True,
    ) -> FileMutation | None:
        """Record post-tool state, merging repeated edits to the same file."""

        if pending is None or not applied or self._active is None:
            return None
        target = self._resolve_workspace_path(pending.path)
        if target is None or self._is_rejected_path(target):
            return None
        after = self._read_file(target)
        if after is _TOO_LARGE:
            return None
        after_bytes = after if isinstance(after, bytes) else None
        after_hash = _sha256(after_bytes)
        after_snapshot = self._save_snapshot(after_bytes)
        if pending.before_hash == after_hash:
            return None

        existing = next(
            (
                mutation
                for mutation in self._active.mutations
                if mutation.path == pending.path
            ),
            None,
        )
        if existing is None:
            mutation = FileMutation(
                path=pending.path,
                kind=_mutation_kind(pending.before_hash, after_hash),
                before_hash=pending.before_hash,
                after_hash=after_hash,
                before_snapshot=pending.before_snapshot,
                after_snapshot=after_snapshot,
                call_ids=[pending.call_id] if pending.call_id else [],
            )
            self._active.mutations.append(mutation)
        else:
            existing.after_hash = after_hash
            existing.after_snapshot = after_snapshot
            existing.kind = _mutation_kind(existing.before_hash, after_hash)
            if pending.call_id and pending.call_id not in existing.call_ids:
                existing.call_ids.append(pending.call_id)
            mutation = existing
        return mutation

    def finalize_turn(self) -> TurnRecord | None:
        """Finalize and atomically persist the active turn."""

        if self._active is None:
            return None
        self._active.finalized_at = _utc_now()
        record = self._active
        self._active = None
        if not record.mutations:
            return record
        self._turns.append(record)
        self._save_index()
        return record

    def list_turns(self) -> list[TurnRecord]:
        return list(self._turns)

    def latest_turn(self) -> TurnRecord | None:
        return self._turns[-1] if self._turns else None

    def preview_rollback(self, turn_id: str | None = None) -> dict[str, Any]:
        """Describe rollback eligibility without changing workspace state."""

        latest = self.latest_turn()
        if latest is None:
            return {
                "ok": False,
                "code": "NO_MUTATION_TURNS",
                "error": "no finalized mutation turns",
                "turn_id": None,
                "revision": None,
                "conflicts": ["no finalized turns"],
                "files": [],
            }
        requested = turn_id or latest.turn_id
        if requested != latest.turn_id:
            return {
                "ok": False,
                "code": "LATEST_TURN_REQUIRED",
                "error": "only the latest turn can be rolled back",
                "turn_id": latest.turn_id,
                "revision": latest.revision,
                "conflicts": ["only the latest turn can be rolled back"],
                "files": [],
            }

        conflicts: list[str] = []
        files: list[dict[str, Any]] = []
        for mutation in latest.mutations:
            target = self._resolve_workspace_path(mutation.path)
            if target is None:
                conflicts.append(mutation.path)
                continue
            current_hash = self._current_hash(target)
            conflict = current_hash != mutation.after_hash
            if conflict:
                conflicts.append(mutation.path)
            files.append(
                {
                    "path": mutation.path,
                    "kind": mutation.kind,
                    "before_hash": mutation.before_hash,
                    "after_hash": mutation.after_hash,
                    "current_hash": current_hash,
                    "conflict": conflict,
                }
            )
        return {
            "ok": not conflicts,
            "code": None if not conflicts else "ROLLBACK_CONFLICT",
            "error": None if not conflicts else "files changed after the Code turn",
            "turn_id": latest.turn_id,
            "revision": latest.revision,
            "conflicts": conflicts,
            "files": files,
        }

    def rollback(self, turn_id: str, revision: int) -> dict[str, Any]:
        """Atomically refuse conflicts, otherwise restore the latest turn."""

        latest = self.latest_turn()
        preview = self.preview_rollback(turn_id)
        if latest is None or not preview["ok"]:
            return preview
        if revision != latest.revision:
            return {
                **preview,
                "ok": False,
                "code": "ROLLBACK_REVISION_CHANGED",
                "error": "turn revision changed",
                "conflicts": ["turn revision changed"],
            }

        # All hashes were checked before the first write. Snapshot validation is
        # also completed up front so a missing/corrupt blob cannot half-rollback.
        restorations: list[tuple[Path, bytes | None]] = []
        snapshot_conflicts: list[str] = []
        for mutation in latest.mutations:
            target = self._resolve_workspace_path(mutation.path)
            if target is None:
                snapshot_conflicts.append(mutation.path)
                continue
            content: bytes | None = None
            if mutation.before_hash is not None:
                content = self._load_snapshot(mutation.before_snapshot)
                if content is None or _sha256(content) != mutation.before_hash:
                    snapshot_conflicts.append(mutation.path)
                    continue
            restorations.append((target, content))
        if snapshot_conflicts:
            return {
                **preview,
                "ok": False,
                "code": "ROLLBACK_SNAPSHOT_INVALID",
                "error": "rollback snapshot is missing or invalid",
                "conflicts": snapshot_conflicts,
            }

        for target, content in restorations:
            if content is None:
                if target.exists():
                    target.unlink()
            else:
                _atomic_write_bytes(target, content)
        self._turns.pop()
        self._save_index()
        return {
            "ok": True,
            "code": None,
            "error": None,
            "turn_id": latest.turn_id,
            "revision": revision,
            "conflicts": [],
            "files": preview["files"],
        }

    def _resolve_workspace_path(self, value: str | Path) -> Path | None:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.workspace)
        except (OSError, ValueError):
            return None
        return resolved

    def _is_rejected_path(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.workspace)
        except ValueError:
            return True
        return any(part.lower() in _REJECTED_PARTS for part in relative.parts)

    @staticmethod
    def _read_file(path: Path) -> bytes | None | object:
        try:
            if not path.exists():
                return None
            if not path.is_file() or path.stat().st_size > _MAX_CAPTURE_BYTES:
                return _TOO_LARGE
            return path.read_bytes()
        except OSError:
            return _TOO_LARGE

    def _save_snapshot(self, content: bytes | None) -> str | None:
        digest = _sha256(content)
        if digest is None:
            return None
        relative = Path(digest[:2]) / digest
        target = self.snapshot_root / relative
        if not target.exists():
            _atomic_write_bytes(target, content or b"")
        return relative.as_posix()

    def _load_snapshot(self, relative: str | None) -> bytes | None:
        if not relative:
            return None
        target = (self.snapshot_root / relative).resolve()
        try:
            target.relative_to(self.snapshot_root.resolve())
            return target.read_bytes()
        except (OSError, ValueError):
            return None

    def _current_hash(self, path: Path) -> str | None:
        content = self._read_file(path)
        if content is _TOO_LARGE:
            return "__unreadable__"
        return _sha256(content if isinstance(content, bytes) else None)

    def _read_index(self) -> dict[str, Any]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"invalid mutation store index: {self.index_path}"
            ) from exc
        if not isinstance(data, dict):
            raise ValueError(f"invalid mutation store index: {self.index_path}")
        return data

    def _save_index(self) -> None:
        data = {
            "version": _INDEX_VERSION,
            "workspace": str(self.workspace),
            "turns": [asdict(turn) for turn in self._turns],
        }
        encoded = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode(
            "utf-8"
        )
        _atomic_write_bytes(self.index_path, encoded)

    @staticmethod
    def _turn_from_dict(data: dict[str, Any]) -> TurnRecord:
        mutations = [FileMutation(**mutation) for mutation in data.get("mutations", [])]
        return TurnRecord(
            session_id=str(data.get("session_id", "")),
            turn_id=str(data["turn_id"]),
            task_text=str(data.get("task_text", "")),
            revision=int(data.get("revision", 0)),
            mutations=mutations,
            started_at=str(data.get("started_at", "")),
            finalized_at=data.get("finalized_at"),
        )


def default_mutation_store_root(session_id: str) -> Path:
    """Return the durable mutation root for a safely encoded session id."""

    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id or "")).strip("._-")
    return get_agent_root_dir() / "code_mutations" / (safe_id or "session")


def _extract_path(args: Any) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return ""
    if not isinstance(args, dict):
        return ""
    for key in ("path", "file_path", "filepath", "target_path", "file"):
        value = args.get(key)
        if isinstance(value, (str, os.PathLike)) and str(value).strip():
            return str(value)
    return ""


def _mutation_kind(before_hash: str | None, after_hash: str | None) -> str:
    if before_hash is None:
        return "create"
    if after_hash is None:
        return "delete"
    return "modify"


def _sha256(content: bytes | None) -> str | None:
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


_TOO_LARGE = object()

__all__ = [
    "FileMutation",
    "PendingCapture",
    "TurnMutationStore",
    "TurnRecord",
    "default_mutation_store_root",
]
