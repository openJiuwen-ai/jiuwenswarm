"""Host-owned evidence scope and confirmation snapshots for SkillPack creation."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from filelock import FileLock
from openjiuwen.symphony.flow import FlowStore, RecipeEvidence

from jiuwenswarm.server.runtime.skill.skill_type import resolve_skillpack_member_type


def trusted_session_user(session_id: str) -> str:
    """Read owner identity from Host routing or pinned session metadata."""
    from jiuwenswarm.agents.harness.common.tools.session_messaging_toolkit import current_session_messaging_route
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    route = current_session_messaging_route()
    if route is not None and route.session_id == session_id and route.user_id:
        return route.user_id
    metadata = get_session_metadata(session_id, enable_writeback=False)
    return str(metadata.get("user_id") or "") if isinstance(metadata, dict) else ""


def member_snapshot(skills_root: Path, members: list[str], disabled: list[str]) -> dict[str, str]:
    """Hash ordinary, enabled member content without following any symlink."""
    snapshots: dict[str, str] = {}
    for name in members:
        if not name or name in {".", ".."} or any(char in name for char in "/\\:"):
            raise ValueError("invalid_member")
        directory = skills_root / name
        if directory.is_symlink() or directory.resolve().parent != skills_root.resolve():
            raise ValueError("invalid_member_path")
        if resolve_skillpack_member_type(directory) != "skill":
            raise ValueError("non_ordinary_member")
        if name in disabled:
            raise ValueError("member_disabled")
        digest = hashlib.sha256()
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(directory)
            if relative.parts[0] in {".archive", ".git"}:
                continue
            if path.is_symlink():
                raise ValueError("member_symlink")
            if path.is_file():
                digest.update(relative.as_posix().encode())
                digest.update(b"\0")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        snapshots[name] = digest.hexdigest()
    return snapshots


def annotate_member_types(execution: dict[str, Any], skills_root: Path) -> dict[str, Any]:
    """Add server-resolved member types to a detached real execution graph."""
    annotated = deepcopy(execution)
    graph = annotated.get("graph")
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if not isinstance(nodes, dict):
        return annotated
    for raw_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        name = str(raw_id).removeprefix("skill:").removeprefix("capability:")
        metadata = node.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            continue
        safe_name = bool(name) and name not in {".", ".."} and not any(char in name for char in "/\\:")
        metadata["skill_type"] = (
            resolve_skillpack_member_type(skills_root / name) if safe_name else "unknown"
        )
    return annotated


def candidate_members(recipe: Any) -> list[str]:
    """Return canonical member IDs from a persisted Core recipe."""
    nodes = recipe.combination_structure.get("nodes")
    return list(nodes) if isinstance(nodes, dict) else []


@dataclass(frozen=True)
class CandidateEvidenceScope:
    """Host-resolved identity and the exact optional evidence restriction."""

    session_id: str
    user_id: str
    scope: str
    source_refs: tuple[str, ...] | None = None

    @classmethod
    def resolve(
        cls, session_id: str, user_id: str, scope: str, source_refs: list[str] | None,
    ) -> CandidateEvidenceScope:
        if not session_id or scope not in {"current_user", "current_session"}:
            raise ValueError("invalid_scope")
        return cls(
            session_id, user_id, scope if user_id else "current_session",
            tuple(sorted(set(source_refs))) if source_refs is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def key(self) -> str:
        # Include the session even for user scope: only this session's anonymous
        # evidence can be admitted alongside authenticated same-user evidence.
        encoded = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def allows(self, trace_id: str, record: dict[str, Any]) -> bool:
        same_session = record.get("session_id") == self.session_id
        same_user = bool(self.user_id) and record.get("user_id") == self.user_id
        allowed = (
            same_user or (same_session and not record.get("user_id"))
            if self.scope == "current_user" and self.user_id
            else same_session and (same_user or not record.get("user_id"))
        )
        return allowed and (self.source_refs is None or trace_id in self.source_refs)


class ScopedCandidateStore(FlowStore):
    """Persist derived candidates while reading only eligible original evidence."""

    def __init__(
        self, source: FlowStore, scope: CandidateEvidenceScope, skills_root: Path,
        disabled_for: Callable[[], list[str]],
    ) -> None:
        super().__init__(source.root / "skillpack_candidates" / scope.key)
        self.source = source
        self.scope = scope
        self.skills_root = skills_root
        self.disabled_for = disabled_for

    def append_evidence(self, evidence: RecipeEvidence) -> bool:
        raise ValueError("Scoped candidates cannot create or modify execution evidence")

    def read_evidence(self) -> list[RecipeEvidence]:
        ownership = CandidateScopeStore(self.source.root)._read().get("traces", {})
        snapshots: dict[tuple[str, ...], dict[str, str] | None] = {}
        disabled = self.disabled_for()
        result = []
        for evidence in self.source.read_evidence():
            record = ownership.get(evidence.trace_id, {})
            if not isinstance(record, dict) or not self.scope.allows(evidence.trace_id, record):
                continue
            members = tuple(sorted(evidence.graph.get("nodes", {})))
            if members not in snapshots:
                try:
                    snapshots[members] = member_snapshot(self.skills_root, list(members), disabled)
                except (OSError, ValueError):
                    snapshots[members] = None
            snapshot = snapshots[members]
            if snapshot is not None and snapshot == record.get("members"):
                result.append(evidence)
        return result

    def contains_evidence(self, trace_ids: list[str]) -> bool:
        """A retained recipe is insufficient if its original evidence disappeared."""
        return bool(trace_ids) and set(trace_ids).issubset({item.trace_id for item in self.read_evidence()})


class CandidateScopeStore:
    """Persist trusted trace ownership and approvals in the user's Flow store."""

    def __init__(self, flow_root: Path) -> None:
        self.root = flow_root
        self.path = flow_root / "skillpack_scopes.json"

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"traces": {}, "approvals": {}}
        return payload if isinstance(payload, dict) else {"traces": {}, "approvals": {}}

    def _update(self, section: str, key: str, value: dict[str, Any], *, immutable: bool = False) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path) + ".lock"):
            payload = self._read()
            previous = payload.get(section, {}).get(key)
            if immutable and previous is not None:
                if previous != value:
                    raise ValueError("trace_scope_changed")
                return
            payload.setdefault(section, {})[key] = value
            descriptor, temporary = tempfile.mkstemp(prefix=".skillpack-scopes-", dir=self.root)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def record_trace(self, trace_id: str, session_id: str, snapshot: dict[str, str], user_id: str = "") -> None:
        self._update(
            "traces", trace_id, {"session_id": session_id, "user_id": user_id, "members": snapshot}, immutable=True,
        )

    def scoped_evidence(
        self, recipe: Any, session_id: str, snapshot: dict[str, str], *,
        user_id: str = "", scope: str = "current_user", source_refs: list[str] | None = None,
    ) -> list[str]:
        traces = recipe.provenance.get("evidence_trace_ids") or []
        records = self._read().get("traces", {})
        if not traces or not session_id:
            return []
        identity = CandidateEvidenceScope.resolve(session_id, user_id, scope, source_refs)
        for trace_id in traces:
            record = records.get(trace_id, {})
            if not identity.allows(trace_id, record) or record.get("members") != snapshot:
                return []
        return list(traces)

    def grant(self, request_id: str, approval: dict[str, Any]) -> None:
        self._update("approvals", request_id, approval)

    def approval(self, request_id: str) -> dict[str, Any] | None:
        value = self._read().get("approvals", {}).get(request_id)
        return value if isinstance(value, dict) else None


def candidate_request_id(recipe_id: str, version: int, session_id: str, scope_key: str = "") -> str:
    digest = hashlib.sha256(f"{session_id}:{recipe_id}:{version}:{scope_key}".encode()).hexdigest()[:20]
    return f"symphony_experience_tool_{digest}"
